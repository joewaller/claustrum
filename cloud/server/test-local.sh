#!/usr/bin/env bash
# Local end-to-end smoke test for the cloud server.
# Spins up Postgres in Docker, applies migration, starts uvicorn, hits a few
# endpoints, tears down. Use before pushing to verify the scaffold works.
#
# Requirements: Docker Desktop running, Python 3.11+, .venv set up.
# First-time setup:
#   python3 -m venv .venv && .venv/bin/pip install -e .
#
# Usage: ./test-local.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONTAINER=claustrum-pg-test
PORT_PG=55432
PORT_API=58080
DB_URL="postgresql://postgres:dev@localhost:${PORT_PG}/claustrum"
PYBIN=".venv/bin/python"

cleanup() {
  echo "--- cleanup ---"
  if [[ -n "${API_PID:-}" ]]; then kill "$API_PID" 2>/dev/null || true; fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [[ ! -x "$PYBIN" ]]; then
  echo "no venv at $PYBIN — run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

echo "--- starting postgres in docker ($CONTAINER on port $PORT_PG) ---"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -p "${PORT_PG}:5432" \
  -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=claustrum \
  -e POSTGRES_USER=postgres \
  postgres:16 >/dev/null

echo "--- waiting for postgres ---"
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d claustrum 2>/dev/null | grep -q accepting; then
    echo "postgres ready in ${i}s"
    break
  fi
  sleep 1
done

echo "--- applying migration ---"
docker exec -i "$CONTAINER" psql -U postgres -d claustrum -v ON_ERROR_STOP=1 \
  < migrations/0001_init.sql

echo "--- verifying schema ---"
docker exec "$CONTAINER" psql -U postgres -d claustrum -c "\dt"

echo "--- starting uvicorn on port $PORT_API ---"
CLAUSTRUM_DB_URL="$DB_URL" \
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

run "POST /v1/checkin again (idempotent — expect 200, topic_required=true)" \
  -X POST "http://localhost:${PORT_API}/v1/checkin" \
  -H 'Content-Type: application/json' \
  -H 'X-Claustrum-User-Email: joe@finder.com' \
  -d '{"uid":"test-1","machine":"joe-mbp","label":"smoke-test"}'

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
docker exec "$CONTAINER" psql -U postgres -d claustrum \
  -c "SELECT uid, user_email, machine, repo, topic, status, pr_number, files_touched FROM sessions ORDER BY uid"

echo
echo "--- ALL TESTS PASSED ---"
