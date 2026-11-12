#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Demo data and credentials are isolated from the production .env.
export API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export TELEGRAM_ENABLED=false
export DATABASE_URL=sqlite+aiosqlite:///./.hoplite/demo.db
export LEADS_API_URL=http://127.0.0.1:8000/api/leads

pids=()
cleanup() {
  kill "${pids[@]}" 2>/dev/null || true
  wait "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers &
pids+=("$!")
uv run uvicorn examples.site:app --host 0.0.0.0 --port 3000 --workers 1 --no-proxy-headers &
pids+=("$!")
wait -n "${pids[@]}"
