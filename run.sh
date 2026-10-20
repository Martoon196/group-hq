#!/usr/bin/env sh
# Local development: loads .env (if present), migrates to head on boot, serves on http://127.0.0.1:8030.
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
if [ -z "$DATABASE_URL" ]; then
  echo "DATABASE_URL is not set. Group HQ needs a Postgres database; see .env.example." >&2
  exit 1
fi
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" -m uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8030}"
