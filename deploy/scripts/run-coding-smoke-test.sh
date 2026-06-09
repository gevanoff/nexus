#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
BASE_URL="${NEXUS_GATEWAY_URL:-http://127.0.0.1:8800}"

if [[ -z "${NEXUS_CODING_SMOKE_OUTPUT_DIR:-}" ]]; then
  if [[ -d "/ai-data" ]]; then
    NEXUS_CODING_SMOKE_OUTPUT_DIR="/ai-data/var/lib/nexus-smoke/coding"
  else
    NEXUS_CODING_SMOKE_OUTPUT_DIR="${NEXUS_RUNTIME_ROOT:-$ROOT_DIR/.runtime}/coding-smoke"
  fi
  export NEXUS_CODING_SMOKE_OUTPUT_DIR
fi

exec python3 "$ROOT_DIR/deploy/scripts/coding-agent-smoke-test.py" \
  --base-url "$BASE_URL" \
  --env-file "$ENV_FILE" \
  "$@"
