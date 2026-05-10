# bootstrap_taxonomy

One-time script that seeds the `topics` table from your project's recent
activity. Runs locally; outputs a JSON file you check into source control.

The taxonomy shapes how every future session is tagged, so the output should
be human-reviewable. The JSON is the source of truth; the SQL companion is
generated from it for migration purposes.

## What it pulls

| Source | Required | Notes |
|---|---|---|
| GitHub merged PRs (last 90 days) | yes | via `gh` CLI; titles + first 1500 chars of body |
| work-history entries (last 90 days) | optional | via HTTP `--include-work-history URL` |
| KG entities by domain | optional | via HTTP, repeatable `--include-kg-domain DOMAIN`; requires `CLAUSTRUM_KG_URL` and optionally `CLAUSTRUM_KG_TOKEN` |

## What it does

1. Concatenates all artefacts.
2. Hands them to one LLM call (`--llm gemini|claude|local`).
3. Asks for 30–50 named topics with descriptions.
4. Writes `seed_topics.json` (and optionally `0002_seed_topics.sql`).

## Quick start

OSS user with their own GitHub org:

```
./bootstrap_taxonomy.py --github-org your-org --llm claude --out seed_topics.json
```

Reviewing the output:

```
cat seed_topics.json | jq '.[].name'
```

Edit `seed_topics.json` if you want to tweak the clustering — it's the source
of truth for both code review and the SQL migration.

## Generating the SQL migration

```
./bootstrap_taxonomy.py --github-org your-org --llm claude \
    --out cloud/server/seed_topics.json \
    --out-sql cloud/server/migrations/0002_seed_topics.sql
```

Then commit both files. The deploy applies the migration on next deploy.

## LLM provider auth

| Provider | Env var |
|---|---|
| `gemini` | `GEMINI_API_KEY` |
| `claude` | `ANTHROPIC_API_KEY` |
| `local`  | `LOCAL_LLM_URL` (OpenAI-compatible endpoint) |

Note: the scaffold currently raises `NotImplementedError` from each provider's
call site — the wiring lands in a follow-up PR.

## Re-running

The taxonomy isn't static. Re-run quarterly (or whenever cluster composition
drifts) to refresh. Daily `recluster` and `topic-merge` jobs in the running
server handle routine churn between bootstrap runs.
