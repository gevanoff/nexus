#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd curl

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:${GATEWAY_PORT:-8800}}"
OBS_URL="${GATEWAY_OBS_URL:-http://127.0.0.1:${OBSERVABILITY_PORT:-8801}}"

TOKEN="${GATEWAY_BEARER_TOKEN:-}"
if [[ -z "${TOKEN}" && -f "${ENV_FILE}" ]]; then
  TOKEN="$(grep -E '^GATEWAY_BEARER_TOKEN=' "${ENV_FILE}" | head -n 1 | cut -d '=' -f2-)"
fi

if [[ -z "${TOKEN}" ]]; then
  ns_die "GATEWAY_BEARER_TOKEN is not set (set env var or put it in ${ENV_FILE})."
fi

ns_print_header "Gateway Smoke Test"
echo "Base URL: ${BASE_URL}"
echo "Observability URL: ${OBS_URL}"

echo "[1/4] GET /health (observability)"
curl -fsS "${OBS_URL}/health" >/dev/null

echo "[2/4] GET /v1/models"
curl -fsS "${BASE_URL}/v1/models" -H "Authorization: Bearer ${TOKEN}" >/dev/null

echo "[3/4] POST /v1/embeddings"
curl -fsS "${BASE_URL}/v1/embeddings" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"default","input":"smoke test"}' \
  >/dev/null

echo "[4/4] POST /v1/responses (non-stream)"
curl -fsS "${BASE_URL}/v1/responses" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","input":"smoke test","stream":false}' \
  >/dev/null

ns_print_ok "Smoke tests passed"
