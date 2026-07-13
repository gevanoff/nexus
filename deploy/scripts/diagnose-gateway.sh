#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
CONTAINER="${GATEWAY_CONTAINER_NAME:-nexus-gateway}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/diagnose-gateway.sh [--env-file PATH] [--container NAME]

Diagnostics for the Nexus gateway stack.

Options:
  --env-file PATH   Env file path (default: ./.env)
  --container NAME  Gateway container name (default: nexus-gateway)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
        ns_die "--env-file requires a value"
      fi
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --container)
      if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
        ns_die "--container requires a value"
      fi
      CONTAINER="${2:-}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

if [[ ! -f "${ENV_FILE}" ]]; then
  ns_die "Env file not found: ${ENV_FILE}"
fi

resolve_base_url() {
  local env_key="$1"
  local fallback="$2"
  local value
  value="${!env_key:-$(ns_env_get "${ENV_FILE}" "$env_key" "$fallback")}"
  value="${value%/}"
  if [[ "$value" == "http://host.docker.internal"* ]]; then
    value="${value/host.docker.internal/127.0.0.1}"
  fi
  echo "$value"
}

vllm_base_url="$(resolve_base_url VLLM_BASE_URL "http://127.0.0.1:8000/v1")"
vllm_fast_base_url="$(resolve_base_url VLLM_FAST_BASE_URL "http://127.0.0.1:8001/v1")"
vllm_embeddings_base_url="$(resolve_base_url VLLM_EMBEDDINGS_BASE_URL "http://127.0.0.1:8002/v1")"
mlx_base_url="$(resolve_base_url MLX_BASE_URL "")"

gateway_port="${GATEWAY_PORT:-$(ns_env_get "${ENV_FILE}" GATEWAY_PORT 8800)}"
obs_port="${OBSERVABILITY_PORT:-$(ns_env_get "${ENV_FILE}" OBSERVABILITY_PORT 8801)}"
BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:${gateway_port}}"
OBS_URL="${GATEWAY_OBS_URL:-http://127.0.0.1:${obs_port}}"
TOKEN="${GATEWAY_BEARER_TOKEN:-$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")}"
strong_model="${VLLM_MODEL_STRONG:-$(ns_env_get "${ENV_FILE}" VLLM_MODEL_STRONG "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic")}"
fast_model="${VLLM_MODEL_FAST:-$(ns_env_get "${ENV_FILE}" VLLM_MODEL_FAST "cyankiwi/Devstral-Small-2507-AWQ-4bit")}"
embeddings_model="${VLLM_MODEL_EMBEDDINGS:-$(ns_env_get "${ENV_FILE}" VLLM_MODEL_EMBEDDINGS "BAAI/bge-small-en-v1.5")}"

rc=0
mark_fail() { rc=1; }

print_step() {
  echo
  ns_print_header "$1"
}

http_check() {
  local label="$1"
  local method="$2"
  local url="$3"
  local with_auth="$4"
  local payload="${5:-}"
  local tmp status

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
  mark_fail
  return 1
}

model_present() {
  local model="$1"
  local json_payload="$2"
  python3 - "$model" "$json_payload" <<'PY'
import json
import sys

target = sys.argv[1]
payload = sys.argv[2]

try:
    data = json.loads(payload)
except Exception:
    sys.exit(1)

for item in data.get("data", []):
    if str((item or {}).get("id", "")).strip() == target:
        sys.exit(0)

sys.exit(1)
PY
}

probe_models() {
  local label="$1"
  local base_url="$2"
  local expected_model="$3"
  local models_json

  models_json="$(curl -fsS "${base_url%/}/models" 2>/dev/null || true)"
  if [[ -z "$models_json" ]]; then
    ns_print_error "${label} /models probe failed: ${base_url%/}/models"
    mark_fail
    return
  fi

  ns_print_ok "${label} /models probe succeeded"
  if model_present "$expected_model" "$models_json"; then
    ns_print_ok "${label} model is advertised: ${expected_model}"
  else
    ns_print_error "${label} model not advertised: ${expected_model}"
    mark_fail
  fi
}

print_step "Gateway diagnostics"
echo "Repo root: ${ROOT_DIR}"
echo "Env file: ${ENV_FILE}"
echo "Base URL: ${BASE_URL}"
echo "Observability URL: ${OBS_URL}"

print_step "Host prerequisites"
if ns_have_cmd docker; then
  ns_print_ok "docker CLI found"
else
  ns_print_error "docker CLI not found"
  mark_fail
fi

if ns_have_cmd curl && ns_have_cmd python3; then
  ns_print_ok "curl and python3 found"
else
  ns_print_error "curl and python3 are required for endpoint diagnostics"
  mark_fail
fi

if docker info >/dev/null 2>&1; then
  ns_print_ok "Docker daemon reachable"
else
  ns_print_error "Docker daemon not reachable"
  mark_fail
fi

print_step "Gateway container state"
container_status="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)"
container_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$container_status" == "running" ]]; then
  ns_print_ok "${CONTAINER} is running (health: ${container_health:-unknown})"
else
  ns_print_error "${CONTAINER} is not running (status: ${container_status:-missing})"
  mark_fail
fi

print_step "Gateway HTTP checks"
http_check "GET ${OBS_URL}/health" "GET" "${OBS_URL}/health" "false"
if [[ -z "${TOKEN}" ]]; then
  ns_print_error "GATEWAY_BEARER_TOKEN not found"
  mark_fail
else
  ns_print_ok "Bearer token present"
fi
http_check "GET ${BASE_URL}/v1/models" "GET" "${BASE_URL}/v1/models" "true"
http_check "GET ${BASE_URL}/v1/gateway/status" "GET" "${BASE_URL}/v1/gateway/status" "true"
http_check "GET ${OBS_URL}/health/upstreams" "GET" "${OBS_URL}/health/upstreams" "false"
http_check "POST ${BASE_URL}/v1/embeddings" "POST" "${BASE_URL}/v1/embeddings" "true" '{"model":"default","input":"diagnose"}'

print_step "vLLM upstream checks"
probe_models "strong vLLM" "${vllm_base_url}" "${strong_model}"
probe_models "fast vLLM" "${vllm_fast_base_url}" "${fast_model}"
probe_models "embeddings vLLM" "${vllm_embeddings_base_url}" "${embeddings_model}"

if [[ -n "$mlx_base_url" ]]; then
  print_step "Optional MLX upstream check"
  mlx_models_json="$(curl -fsS "${mlx_base_url%/}/models" 2>/dev/null || true)"
  if [[ -n "$mlx_models_json" ]]; then
    ns_print_ok "MLX /models probe succeeded"
  else
    ns_print_warn "MLX /models probe failed (${mlx_base_url%/}/models)"
  fi
fi

echo
if [[ "$rc" -eq 0 ]]; then
  ns_print_ok "Gateway diagnostics passed"
else
  ns_print_error "Gateway diagnostics found issues"
fi

exit "$rc"
