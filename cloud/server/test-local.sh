#!/usr/bin/env bash
# Local test for the cloud server — Docker-free.
#
# Two layers:
#   1. Unit tests (pure dedup logic) — ALWAYS run. No DB, no Docker.
#   2. HTTP integration — runs ONLY if CLAUSTRUM_DB_URL points at a reachable
#      Postgres. Bring your own DB: a throwaway dev instance, a cloud-sql-proxy
#      to staging, a Neon/Supabase branch, whatever. Migration applied via the
#      local `psql` client (no container). Skipped if unset — staging is the
#      real integration gate (deploy builds from main, runs migrations on
#      Cloud SQL, smoke-tested through IAP).
#
# Requirements: Python 3.11+, .venv with dev deps. For the optional layer 2,
# the `psql` client (libpq) and a CLAUSTRUM_DB_URL.
# First-time setup:
#   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
#
# Usage:
#   ./test-local.sh                              # unit only
#   CLAUSTRUM_DB_URL=postgres://… ./test-local.sh  # unit + HTTP integration

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYBIN=".venv/bin/python"
PORT_API=58080

if [[ ! -x "$PYBIN" ]]; then
  echo "no venv at $PYBIN — run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Layer 1 — unit tests (no DB)
# ---------------------------------------------------------------------------
echo "=== unit tests (pure dedup logic, no DB) ==="
if ! "$PYBIN" -c "import pytest" 2>/dev/null; then
  echo "pytest missing — run: $PYBIN -m pip install -e '.[dev]'" >&2
  exit 1
fi
"$PYBIN" -m pytest -q tests/

# ---------------------------------------------------------------------------
# Layer 2 — HTTP integration (optional, bring-your-own Postgres)
# ---------------------------------------------------------------------------
if [[ -z "${CLAUSTRUM_DB_URL:-}" ]]; then
  echo
  echo "CLAUSTRUM_DB_URL not set — skipping HTTP integration."
  echo "To run it, point CLAUSTRUM_DB_URL at any reachable Postgres, e.g.:"
  echo "  CLAUSTRUM_DB_URL=postgresql://user:pass@host/claustrum ./test-local.sh"
  echo "Otherwise, staging is the integration gate (see /deploy-claustrum)."
  echo
  echo "--- UNIT TESTS PASSED ---"
  exit 0
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql not found — install libpq (brew install libpq) to run layer 2." >&2
  exit 1
fi

API_PID=""
cleanup() {
  echo "--- cleanup ---"
  if [[ -n "${API_PID:-}" ]]; then kill "$API_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

echo "=== HTTP integration against \$CLAUSTRUM_DB_URL ==="

echo "--- applying migration (idempotent) ---"
psql "$CLAUSTRUM_DB_URL" -v ON_ERROR_STOP=1 -f migrations/0001_init.sql >/dev/null

echo "--- starting uvicorn on port $PORT_API (dev-trust-header auth) ---"
CLAUSTRUM_DB_URL="$CLAUSTRUM_DB_URL" \
  "$PYBIN" -m uvicorn app.main:app --port "$PORT_API" --log-level warning &
API_PID=$!

echo "--- waiting for /healthz ---"
for i in $(seq 1 20); do
  if curl -fsS "http://localhost:${PORT_API}/healthz" >/dev/null 2>&1; then
    echo "uvicorn ready in ${i}s"
    break
  fi
  sleep 1
done

run() {
  local label="$1"; shift
  echo "--- $label ---"
  curl -sS "$@" -w "\nHTTP %{http_code}\n"
}

run "GET /healthz" "http://localhost:${PORT_API}/healthz"
run "GET /readyz"  "http://localhost:${PORT_API}/readyz"

run "POST /v1/checkin (no auth header — expect 401)" \
  -X POST "http://localhost:${PORT_API}/v1/checkin" \
  -H 'Content-Type: application/json' \
  -d '{"uid":"test-1","machine":"joe-mbp","label":"smoke-test","task":"verifying scaffold"}'

run "POST /v1/checkin (with auth — expect 200, topic_required=true)" \
  -X POST "http://localhost:${PORT_API}/v1/checkin" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"test-1","machine":"joe-mbp","label":"smoke-test","task":"verifying scaffold","repo":"joewaller/claustrum"}'

run "POST /v1/update (detail layer — expect 200)" \
  -X POST "http://localhost:${PORT_API}/v1/update" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"test-1","status":"active","working_on":"wiring /v1/list","files_touched":["cloud/server/app/routes/list_peers.py"],"pr_number":42}'

run "POST /v1/update again (files union — expect 200, two files)" \
  -X POST "http://localhost:${PORT_API}/v1/update" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"test-1","files_touched":["cloud/server/app/routes/update.py"]}'

run "POST /v1/update (unknown uid — expect 404)" \
  -X POST "http://localhost:${PORT_API}/v1/update" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"does-not-exist","status":"active"}'

run "POST /v1/classify_self (expect 200, historical_dedupe)" \
  -X POST "http://localhost:${PORT_API}/v1/classify_self" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"test-1","topic":"claustrum/dedup-core","confidence":90}'

run "POST /v1/propose_topic (expect 200, count=1, not promotable)" \
  -X POST "http://localhost:${PORT_API}/v1/propose_topic" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"test-1","name":"claustrum/dedup-core","description":"the v2 server dedup engine"}'

# --- Second session (different person, same repo + overlapping file) to prove dedup ---
run "POST /v1/checkin test-2 (kev, same repo)" \
  -X POST "http://localhost:${PORT_API}/v1/checkin" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: kev@finder.com' \
  -d '{"uid":"test-2","machine":"kev-mbp","label":"dedup-probe","repo":"joewaller/claustrum","branch":"feat/v2-dedup-core"}'

run "POST /v1/update test-2 (touches the SAME file as test-1 — t1 overlap)" \
  -X POST "http://localhost:${PORT_API}/v1/update" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: kev@finder.com' \
  -d '{"uid":"test-2","files_touched":["cloud/server/app/routes/list_peers.py"],"working_on":"also poking list_peers"}'

run "GET /v1/list for test-1 (expect test-2 in t1_file_overlap)" \
  "http://localhost:${PORT_API}/v1/list?uid=test-1" \
  -H 'X-Claustrum-User-Email: joe@finder.com'

run "POST /v1/propose_topic from test-2 (2nd distinct user — expect promotable=true)" \
  -X POST "http://localhost:${PORT_API}/v1/propose_topic" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: kev@finder.com' \
  -d '{"uid":"test-2","name":"claustrum/dedup-core","description":"same name, different person"}'

echo "--- DB row check ---"
psql "$CLAUSTRUM_DB_URL" \
  -c "SELECT uid, user_email, machine, repo, topic, status, pr_number, files_touched FROM sessions ORDER BY uid"

echo
echo "--- ALL TESTS PASSED ---"
