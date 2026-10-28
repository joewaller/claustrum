# claustrum-cloud server

FastAPI HTTP server backing `claustrum.<your-domain>`. Postgres-backed.
Designed to sit behind an authenticated proxy that sets `X-Claustrum-User-Email`.

## Testing (Docker-free)

`./test-local.sh` has two layers and no Docker dependency:

1. **Unit tests** — pure dedup logic (`tests/`), always run, no DB.
2. **HTTP integration** — runs only if you point `CLAUSTRUM_DB_URL` at a
   reachable Postgres (bring your own: a throwaway dev instance, a
   `cloud-sql-proxy` to staging, a Neon/Supabase branch…). The migration is
   applied with the local `psql` client. Skipped otherwise — **staging is the
   real integration gate** (see `/deploy-claustrum`).

```
cd cloud/server
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
./test-local.sh                                    # unit only
CLAUSTRUM_DB_URL=postgresql://… ./test-local.sh    # unit + HTTP integration
```

## Run the server locally

You need a Postgres to point at — any reachable instance works (no local
container required). Then:

```
cd cloud/server
pip install -e '.[dev]'
psql "$CLAUSTRUM_DB_URL" -f migrations/0001_init.sql
CLAUSTRUM_DEV_TRUST_HEADER=1 \
uvicorn app.main:app --reload --port 8080
```

In dev mode (`CLAUSTRUM_DEV_TRUST_HEADER=1`), the auth dependency reads
`X-Claustrum-User-Email` directly from the request — useful for local testing
without a proxy. Production must NOT set this; instead deploy behind a real
authenticated proxy.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness — no DB |
| GET | `/readyz` | Readiness — `SELECT 1` |
| POST | `/v1/checkin` | Register or refresh a session |
| POST | `/v1/update` | Update task / working_on / status / files / PR (detail layer) |
| GET | `/v1/list` | Per-turn peer query — tiered overlap dedup, server-side filtered; also returns matching `solved` work and other people's open `intents` (`include_intents=false` to skip) |
| POST | `/v1/intents` | Declare planned work (title, repo, paths, topic, uid, PR). Idempotent per person+repo+title (re-declaring unions paths, refreshes expiry) |
| POST | `/v1/intents/{id}` | Edit your intent (title, paths, status, PR, `refresh` expiry) |
| POST | `/v1/intents/{id}/close` | Close your intent as `done` (default) or `abandoned` |
| POST | `/v1/intents/close-linked` | A PR merged: close your session-owned open intents for `uid`, or recorded against `repo` + `pr_number` (session-less intents are left open) |
| GET | `/v1/intents` | Browse open intents (`mine`, `repo`, `topic`, `include_closed`); others' private intents are never returned |
| POST | `/v1/claim` | Soft, TTL'd cross-machine file claim — returns live peer conflicts |
| POST | `/v1/release` | Release a claim |
| POST | `/v1/classify_self` | Set topic (+ `domain`, else derived from the topic) on the session + return historical dedupe |
| POST | `/v1/propose_topic` | Add a topic canonically NOW unless a similarity-guard near-duplicate exists (then `mapped_to` it); `domain` (default `general`) places it |
| POST | `/v1/propose_domain` | Add a domain canonically NOW unless a near-duplicate exists (then `mapped_to` it) |
| GET | `/v1/archive` | Browse the solved-problem archive — all `done` resolution-bearing sessions, any age, paginated (limit/offset, hot+cold) |
| GET | `/v1/resume_check` | What changed while paused (peer activity, merged PRs, expired claims) |
| GET | `/v1/inbox_drain` | Atomically fetch + mark-delivered pending messages |
| POST | `/v1/reset` | Per-user wipe (sessions, proposals, intents, own claims + sent messages) |
| POST | `/jobs/state-transitions` | Cloud Scheduler — 5-min (active→paused for dead / >24h abandoned sessions, expire claims, purge intents closed/lapsed >90d) |
| POST | `/jobs/topic-concentration` | Cloud Scheduler — hourly (≥3 active on a topic → alert) |
| POST | `/jobs/validate-proposals` | Cloud Scheduler — hourly proposal-table cleanup (promotion now happens inline at propose time; this resolves any legacy/open rows) |
| POST | `/jobs/archive-cold` | Cloud Scheduler — daily (move cold rows → `sessions_archive`; copy-not-delete, no BQ) |
| POST | `/jobs/dedupe-digest` | Cloud Scheduler — hourly (**501 — deferred**) |
| POST | `/jobs/recluster` | Cloud Scheduler — daily (**501 — deferred; server is LLM-free**) |
| POST | `/jobs/topic-merge` | Propose domain/topic merges into `taxonomy_aliases` (`status='proposed'` only — never applied). `?dry_run=true` returns the plan without writing. Not yet on a Cloud Scheduler trigger |

OpenAPI doc at `/docs` when running.

`/jobs/*` skip `current_user` and are reachable only via Cloud Scheduler OIDC
tokens through the IAP-protected LB (Cloud Run internal-LB ingress + IAP + IAM)
— never unauthenticated. The two `501`s are deliberately deferred maintenance,
not missing core: topic assignment happens at the source via `classify_self`.

### Reviewing taxonomy merges

`/jobs/topic-merge` only ever stores proposals. Approved rows in
`taxonomy_aliases` (migration 0013) are honoured at read time by `/v1/list`
(alias-aware t3 + the t3b related tier), `/v1/classify_self`, `propose_*` and
the `alias_of` field the classifier uses to drop variants — history is never
rewritten. `/v1/list` caches the alias map per instance for 60s, so an approval
reaches the overlap tiers within a minute. Each cluster's canonical is the
most-used name (adopters, then sessions); on a usage tie the Australian
spelling wins (artefacts over artifacts, -ise over -ize, colour over color).
`propose_*` dedup applies the same spelling preference when two existing names
match equally.

Proposal rules: a near-duplicate **name** (spelling/suffix variants —
`artifacts-site -> artifacts`, `contracts-review -> contract-review`) is always
proposed. **related** (shared stem / name tokens) and **adopters** (unrelated
names) rest on adopter-set Jaccard, so they need at least
`MIN_ADOPTERS_FOR_ADOPTER_MATCH` (3) distinct adopters on *each* side — with 1–2
people a Jaccard of 1.0 is trivial. The plan is never truncated: `plan` and the
job list every qualifying pair.

Review and decide with `CLAUSTRUM_DB_URL` pointed at the target DB:

```
python -m app.merge_cli plan                 # read-only dry run, grouped by canonical
python -m app.merge_cli plan --json > plan.json   # edit down to the merges you accept
python -m app.merge_cli pending              # proposals the job has stored
python -m app.merge_cli approve-plan plan.json --by you@finder.com --yes
python -m app.merge_cli approve-plan --live --only-reason name --by you@finder.com        # dry run
python -m app.merge_cli approve-plan --live --only-reason name --by you@finder.com --yes  # writes
python -m app.merge_cli approve --kind topic artifacts artefacts --by you@finder.com --yes
python -m app.merge_cli reject  --kind topic artifacts --by you@finder.com --yes
```

`approve-plan` takes a PLAN.json **or** `--live` (builds the plan from the DB
in-process). `--only-reason R` (repeatable; any match) keeps proposals whose
`reasons` include `R` (`name`, `related`, `adopters`, `via-cluster`);
`--kind domain|topic|all` (default `all`). Without `--yes` it prints one
`would approve` / `would skip` line per merge and writes nothing; with `--yes`
it writes in one transaction and prints `approved` / `skip`. Every merge goes
through `check_decision` (canonical must exist, no loops — including loops with
earlier merges in the same batch).

**Against prod.** Cloud SQL is private-IP only, so run the CLI as the
`claustrum-<env>-admin` Cloud Run Job (terraform output `admin_job_name`;
command fixed to `python -m app.merge_cli`, default args `pending`). Override
args per run — comma-separated:

```
gcloud run jobs execute claustrum-prod-admin --region=australia-southeast1 --wait --args=plan
gcloud run jobs execute claustrum-prod-admin --region=australia-southeast1 --wait \
  --args=approve-plan,--live,--only-reason,name,--by,you@finder.com
gcloud run jobs execute claustrum-prod-admin --region=australia-southeast1 --wait \
  --args=approve-plan,--live,--only-reason,name,--by,you@finder.com,--yes
```

Output lands in Cloud Logging (`resource.type="cloud_run_job"`, filter on the
execution name `--wait` prints) — see `cloud/terraform/README.md`.

`/jobs/archive-cold` bounds the hot `sessions` table by moving `done` rows
older than 180d and `paused` rows stale >30d into `sessions_archive` (same
Postgres DB — there is no BigQuery; the old `archive-to-bq` spec is retired).
Rows are kept forever and stay readable: the solved nudge and `/v1/archive`
read `v_sessions_all`, which unions hot + cold. Paused rows keep `paused`
status — they are not auto-closed to `done`.
See the "Claustrum deferred jobs" rationale (recluster / topic-merge /
dedupe-digest) before building them.

### Intents

`migrations/0012_intents.sql` adds the `intents` table (additive — old clients
never write it and ignore the extra `intents` key on `/v1/list`). An intent is
a title plus repo-relative directory prefixes (globs are cut back to their
static prefix), owned by a person and optionally a session. `/v1/list` matches
other people's open, unexpired, non-private intents against the caller's repo
+ files (shared directory, either side an ancestor) or topic, **without** the
`recency_min` bound. An intent tied to a private session inherits its privacy.
A `/v1/update` that marks a session `done` also closes that session's open
intents; intents with no owning session are only ever closed by their owner
or by expiry.
Default TTL 30 days (1–180), refreshable.

The DB-backed tests in `tests/test_intents.py` run when
`CLAUSTRUM_TEST_DB_URL` points at a throwaway Postgres (migrations are applied
by the fixture); otherwise they skip.

## Environment variables

| Var | Purpose |
|---|---|
| `CLAUSTRUM_DB_URL` | Postgres connection string. Required. |
| `CLAUSTRUM_DEV_TRUST_HEADER` | If `1`, read `X-Claustrum-User-Email` directly. **Dev only.** |
| `CLAUSTRUM_AUTH_HEADER` | Header name to read for the authenticated user email. Default `X-Claustrum-User-Email`. Operators with proxies that emit a different header (e.g. IAP's `X-Goog-Authenticated-User-Email`) can point this at the proxy's header instead. |
| `PORT` | HTTP port. Default 8080. |
