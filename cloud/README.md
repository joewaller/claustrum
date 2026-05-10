# Claustrum Cloud — optional cross-machine companion

This directory is the optional cloud companion to the single-file `claustrum`
CLI at the repo root. It provides cross-machine coordination for teams running
multiple AI-assisted sessions across laptops and VMs.

The single-file CLI at the repo root stays zero-dep and works without any of
this — point sessions at a cloud server only if you want awareness across
machines.

## What's in here

| Path | Purpose |
|---|---|
| `server/` | FastAPI HTTP server. Postgres-backed. Container-deployable. |
| `client/` | Stdlib-only Python HTTP client that the root `claustrum` script imports when `CLAUSTRUM_CLOUD_URL` is set. |
| `bootstrap/` | One-time topic-taxonomy seeding script. |
| `terraform/` | Example terraform for a GCP deployment behind IAP. Adapt for your environment. |

## How to run your own

Short version:

1. Stand up a Postgres 16 instance.
2. Apply `server/migrations/0001_init.sql`.
3. Run the bootstrap script against your GitHub org to seed `seed_topics.json`,
   then apply the generated `0002_seed_topics.sql`.
4. Build the server container; deploy it behind an authenticated proxy that
   sets the `X-Claustrum-User-Email` header.
5. Set `CLAUSTRUM_CLOUD_URL=https://your-server` in the environment of any
   machine running `claustrum`.

See `server/README.md`, `bootstrap/README.md`, and `terraform/README.md` for
the long versions.

## Auth model

The server has no auth code. It trusts an upstream proxy to set
`X-Claustrum-User-Email`. Plug in whatever you already run — GCP IAP, Cloudflare
Access, Tailscale + Authelia, Caddy + basic auth.

## Status

Scaffold. Endpoints are defined and routed; most return 501 with a documented
response shape. Watch the repo for end-to-end implementation.
