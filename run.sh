#!/usr/bin/env bash
set -euo pipefail

# Production-friendly launcher. Put Nginx in front and bind Uvicorn to localhost.
# Set WEB_CONCURRENCY=8 only after benchmarking memory, database pool usage and latency.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-4}"
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"

exec uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WEB_CONCURRENCY" \
  --proxy-headers \
  --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
