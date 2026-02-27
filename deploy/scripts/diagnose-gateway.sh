#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
gateway_port="${GATEWAY_PORT:-}"
obs_port="${OBSERVABILITY_PORT:-}"
if [[ -f "${ENV_FILE}" ]]; then
  gateway_port="${gateway_port:-$(ns_env_get "${ENV_FILE}" GATEWAY_PORT 8800)}"
  obs_port="${obs_port:-$(ns_env_get "${ENV_FILE}" OBSERVABILITY_PORT 8801)}"
fi

gateway_port="${gateway_port:-8800}"
obs_port="${obs_port:-8801}"

BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:${gateway_port}}"
OBS_URL="${GATEWAY_OBS_URL:-http://127.0.0.1:${obs_port}}"
TOKEN="${GATEWAY_BEARER_TOKEN:-}"
if [[ -z "${TOKEN}" && -f "${ENV_FILE}" ]]; then
  TOKEN="$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")"
fi

# SYNC-CHECK(core-compose-files): keep aligned with ops-stack.sh and cutover-one-way.sh.
COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml)

rc=0

mark_fail() {
  rc=1
}

print_step() {
  echo
  ns_print_header "$1"
}

http_check() {
  # Usage: http_check <label> <method> <url> <auth:true|false> [json_payload]
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
    ns_print_ok "$label -> HTTP $status"
    rm -f "$tmp"
    return 0
  fi

  ns_print_error "$label -> HTTP $status"
  if [[ -s "$tmp" ]]; then
    ns_print_warn "Body (first 600 chars):"
    head -c 600 "$tmp" 2>/dev/null || true
    echo
  fi
  rm -f "$tmp"

  if [[ "$status" == "401" || "$status" == "403" ]]; then
    ns_print_warn "Auth failure: token may be wrong for the running gateway instance."
    ns_print_warn "Token source: ${ENV_FILE} (or GATEWAY_BEARER_TOKEN env var)."
  fi

  mark_fail
  return 1
}

print_step "Gateway Diagnostics"
echo "Repo root: ${ROOT_DIR}"
echo "Env file: ${ENV_FILE}"
echo "Base URL: ${BASE_URL}"
echo "Observability URL: ${OBS_URL}"

print_step "Host prerequisites"
if ! ns_have_cmd docker; then
  ns_print_error "docker CLI not found"
  mark_fail
else
  ns_print_ok "docker CLI found"
fi

if ! ns_compose_available; then
  ns_print_error "Docker Compose not available"
  mark_fail
else
  ns_print_ok "Docker Compose available: $(ns_compose_cmd_string)"
fi

if ! docker info >/dev/null 2>&1; then
  ns_print_error "Docker daemon not reachable"
  ns_print_warn "Try: ./deploy/scripts/ops-stack.sh"
  mark_fail
else
  ns_print_ok "Docker daemon reachable"
fi

print_step "Compose files and stack state"
for compose_file in docker-compose.gateway.yml docker-compose.ollama.yml docker-compose.etcd.yml; do
  if [[ -f "$ROOT_DIR/$compose_file" ]]; then
    ns_print_ok "Found $compose_file"
  else
    ns_print_error "Missing $compose_file"
    mark_fail
  fi
done

if ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" config >/dev/null 2>&1; then
  ns_print_ok "Compose config resolves"
else
  ns_print_error "Compose config resolution failed"
  ns_print_warn "Check ENV_FILE (${ENV_FILE}) and compose file references"
  mark_fail
fi

ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" ps || mark_fail

print_step "HTTP endpoint checks"
http_check "GET ${OBS_URL}/health" "GET" "${OBS_URL}/health" "false"

if [[ -z "${TOKEN}" ]]; then
  ns_print_error "GATEWAY_BEARER_TOKEN not found in env or ${ENV_FILE}"
  mark_fail
else
  ns_print_ok "Bearer token present"
fi

http_check "GET ${BASE_URL}/v1/models" "GET" "${BASE_URL}/v1/models" "true"
http_check "POST ${BASE_URL}/v1/embeddings" "POST" "${BASE_URL}/v1/embeddings" "true" '{"model":"default","input":"diagnose"}'

print_step "Verifier run (in-container)"
if [[ -n "${TOKEN}" ]]; then
  if ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T gateway \
    python3 /var/lib/gateway/tools/verify_gateway.py \
    --skip-pytest \
    --base-url "http://127.0.0.1:${gateway_port}" \
    --obs-url "http://127.0.0.1:${obs_port}" \
    --token "${TOKEN}"; then
    ns_print_ok "In-container verifier passed"
  else
    ns_print_error "In-container verifier failed"
    mark_fail
  fi
else
  ns_print_warn "Skipping in-container verifier (missing token)"
fi

print_step "Gateway logs (tail 120)"
ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" logs --tail=120 gateway || true

echo
if [[ "$rc" -eq 0 ]]; then
  ns_print_ok "Gateway diagnostics completed without detected issues"
else
  ns_print_error "Gateway diagnostics found issues"
  exit 1
fi
