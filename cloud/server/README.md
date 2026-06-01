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
| GET | `/v1/list` | Per-turn peer query — tiered overlap dedup, server-side filtered |
| POST | `/v1/claim` | Soft file claim (501 stub) |
| POST | `/v1/release` | Release claim (501 stub) |
| POST | `/v1/classify_self` | Set topic + return historical dedupe |
| POST | `/v1/propose_topic` | Propose new taxonomy topic (promotes at 2 distinct users) |
| GET | `/v1/resume_check` | What changed while paused (501 stub) |
| GET | `/v1/inbox_drain` | Fetch pending events (501 stub) |
| POST | `/v1/reset` | Per-user wipe (501 stub) |
| POST | `/jobs/state-transitions` | Cloud Scheduler — 5-min job (501 stub) |
| POST | `/jobs/topic-concentration` | Cloud Scheduler — hourly (501 stub) |
| POST | `/jobs/validate-proposals` | Cloud Scheduler — hourly (501 stub) |
| POST | `/jobs/dedupe-digest` | Cloud Scheduler — hourly (501 stub) |
| POST | `/jobs/recluster` | Cloud Scheduler — daily (501 stub) |
| POST | `/jobs/topic-merge` | Cloud Scheduler — daily (501 stub) |
| POST | `/jobs/archive-to-bq` | Cloud Scheduler — daily (501 stub) |

OpenAPI doc at `/docs` when running.

## Environment variables

| Var | Purpose |
|---|---|
| `CLAUSTRUM_DB_URL` | Postgres connection string. Required. |
| `CLAUSTRUM_DEV_TRUST_HEADER` | If `1`, read `X-Claustrum-User-Email` directly. **Dev only.** |
| `CLAUSTRUM_AUTH_HEADER` | Header name to read for the authenticated user email. Default `X-Claustrum-User-Email`. Operators with proxies that emit a different header (e.g. IAP's `X-Goog-Authenticated-User-Email`) can point this at the proxy's header instead. |
| `PORT` | HTTP port. Default 8080. |
