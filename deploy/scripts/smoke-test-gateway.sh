#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd curl

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
gateway_port="${GATEWAY_PORT:-}"
obs_port="${OBSERVABILITY_PORT:-}"
if [[ -f "${ENV_FILE}" ]]; then
  gateway_port="${gateway_port:-$(ns_env_get "${ENV_FILE}" GATEWAY_PORT 8800)}"
  obs_port="${obs_port:-$(ns_env_get "${ENV_FILE}" OBSERVABILITY_PORT 8801)}"
fi

BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:${gateway_port:-8800}}"
OBS_URL="${GATEWAY_OBS_URL:-http://127.0.0.1:${obs_port:-8801}}"

TOKEN="${GATEWAY_BEARER_TOKEN:-}"
if [[ -z "${TOKEN}" && -f "${ENV_FILE}" ]]; then
  TOKEN="$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")"
fi

if [[ -z "${TOKEN}" ]]; then
  ns_die "GATEWAY_BEARER_TOKEN is not set (set env var or put it in ${ENV_FILE})."
fi

ns_print_header "Gateway Smoke Test"
echo "Base URL: ${BASE_URL}"
echo "Observability URL: ${OBS_URL}"

run_check() {
  # Usage: run_check <label> <method> <url> <auth:true|false> [json_payload]
  local label="$1"
  local method="$2"
  local url="$3"
  local with_auth="$4"
  local payload="${5:-}"
  local tmp
  local status

  tmp="$(mktemp)"

  if [[ "$method" == "GET" ]]; then
    if [[ "$with_auth" == "true" ]]; then
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "Authorization: Bearer ${TOKEN}" || true)"
    else
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" || true)"
    fi
  else
    if [[ "$with_auth" == "true" ]]; then
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -X "$method" -d "$payload" || true)"
    else
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "Content-Type: application/json" -X "$method" -d "$payload" || true)"
    fi
  fi

  if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
    rm -f "$tmp"
    return 0
  fi

  ns_print_error "${label} failed with HTTP ${status}."
  if [[ -s "$tmp" ]]; then
    ns_print_warn "Response body (first 400 chars):"
    head -c 400 "$tmp" 2>/dev/null || true
    echo
  fi
  rm -f "$tmp"

  if [[ "$status" == "401" || "$status" == "403" ]]; then
    ns_print_warn "Auth rejected. Verify token source and value:"
    ns_print_warn "  - Env var GATEWAY_BEARER_TOKEN"
    ns_print_warn "  - Or ${ENV_FILE} contains matching GATEWAY_BEARER_TOKEN used by gateway container"
    ns_print_warn "  - Then re-run: ./deploy/scripts/diagnose-gateway.sh"
  elif [[ "$status" == "000" ]]; then
    ns_print_warn "No HTTP response from gateway. Check container status and port bindings."
    ns_print_warn "Run: ./deploy/scripts/diagnose-gateway.sh"
  fi

  return 1
}

echo "[1/4] GET /health (observability)"
run_check "GET ${OBS_URL}/health" "GET" "${OBS_URL}/health" "false"

echo "[2/4] GET /v1/models"
run_check "GET ${BASE_URL}/v1/models" "GET" "${BASE_URL}/v1/models" "true"

echo "[3/4] POST /v1/embeddings"
run_check "POST ${BASE_URL}/v1/embeddings" "POST" "${BASE_URL}/v1/embeddings" "true" '{"model":"default","input":"smoke test"}'

echo "[4/4] POST /v1/responses (non-stream)"
run_check "POST ${BASE_URL}/v1/responses" "POST" "${BASE_URL}/v1/responses" "true" '{"model":"fast","input":"smoke test","stream":false}'

ns_print_ok "Smoke tests passed"
