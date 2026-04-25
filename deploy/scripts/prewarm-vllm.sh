#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
CHECK_ONLY="false"
TIMEOUT_SEC="${VLLM_WARMUP_TIMEOUT_SEC:-30}"
STRONG_BASE_URL_OVERRIDE=""
FAST_BASE_URL_OVERRIDE=""
EMBEDDINGS_BASE_URL_OVERRIDE=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/prewarm-vllm.sh [--env-file PATH] [--check-only] [--timeout-sec N]
                                      [--strong-base-url URL] [--fast-base-url URL] [--embeddings-base-url URL]

Checks vLLM availability and optionally sends minimal warmup requests to the configured
strong, fast, and embeddings endpoints.

Defaults are derived from env:
  - VLLM_BASE_URL
  - VLLM_FAST_BASE_URL
  - VLLM_EMBEDDINGS_BASE_URL
  - VLLM_MODEL_STRONG
  - VLLM_MODEL_FAST
  - VLLM_MODEL_EMBEDDINGS
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --check-only)
      CHECK_ONLY="true"
      shift
      ;;
    --timeout-sec)
      TIMEOUT_SEC="${2:-}"
      shift 2
      ;;
    --strong-base-url)
      STRONG_BASE_URL_OVERRIDE="${2:-}"
      shift 2
      ;;
    --fast-base-url)
      FAST_BASE_URL_OVERRIDE="${2:-}"
      shift 2
      ;;
    --embeddings-base-url)
      EMBEDDINGS_BASE_URL_OVERRIDE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

if [[ ! "$TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  ns_die "Invalid --timeout-sec value: ${TIMEOUT_SEC}"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

ns_require_cmd curl || exit 1
ns_require_cmd python3 || exit 1

resolve_base_url() {
  local override="$1"
  local env_key="$2"
  local fallback="$3"
  local value
  if [[ -n "$override" ]]; then
    value="$override"
  else
    value="${!env_key:-$(ns_env_get "$ENV_FILE" "$env_key" "$fallback")}"
  fi
  value="${value%/}"
  if [[ "$value" == "http://host.docker.internal"* ]]; then
    value="${value/host.docker.internal/127.0.0.1}"
  fi
  echo "$value"
}

strong_base_url="$(resolve_base_url "$STRONG_BASE_URL_OVERRIDE" VLLM_BASE_URL "http://127.0.0.1:8000/v1")"
fast_base_url="$(resolve_base_url "$FAST_BASE_URL_OVERRIDE" VLLM_FAST_BASE_URL "http://127.0.0.1:8001/v1")"
embeddings_base_url="$(resolve_base_url "$EMBEDDINGS_BASE_URL_OVERRIDE" VLLM_EMBEDDINGS_BASE_URL "http://127.0.0.1:8002/v1")"

strong_model="${VLLM_MODEL_STRONG:-$(ns_env_get "$ENV_FILE" VLLM_MODEL_STRONG "Qwen/Qwen2.5-7B-Instruct")}"
fast_model="${VLLM_MODEL_FAST:-$(ns_env_get "$ENV_FILE" VLLM_MODEL_FAST "Qwen/Qwen2.5-3B-Instruct")}"
embeddings_model="${VLLM_MODEL_EMBEDDINGS:-$(ns_env_get "$ENV_FILE" VLLM_MODEL_EMBEDDINGS "BAAI/bge-small-en-v1.5")}"

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

check_models_endpoint() {
  local label="$1"
  local url="$2"
  local models_url="${url%/}/models"
  local payload
  payload="$(curl -fsS --max-time "$TIMEOUT_SEC" "$models_url" 2>/dev/null || true)"
  if [[ -z "$payload" ]]; then
    ns_die "Could not reach ${label} models endpoint at ${models_url}"
  fi
  echo "$payload"
}

ns_print_header "Prewarm vLLM"
echo "Strong endpoint: ${strong_base_url} (${strong_model})"
echo "Fast endpoint: ${fast_base_url} (${fast_model})"
echo "Embeddings endpoint: ${embeddings_base_url} (${embeddings_model})"

strong_models_json="$(check_models_endpoint "strong vLLM" "$strong_base_url")"
fast_models_json="$(check_models_endpoint "fast vLLM" "$fast_base_url")"
embeddings_models_json="$(check_models_endpoint "embeddings vLLM" "$embeddings_base_url")"

if model_present "$strong_model" "$strong_models_json"; then
  ns_print_ok "Strong model is advertised: ${strong_model}"
else
  ns_print_warn "Strong model not listed in /models yet: ${strong_model}"
fi

if model_present "$fast_model" "$fast_models_json"; then
  ns_print_ok "Fast model is advertised: ${fast_model}"
else
  ns_print_warn "Fast model not listed in /models yet: ${fast_model}"
fi

if model_present "$embeddings_model" "$embeddings_models_json"; then
  ns_print_ok "Embeddings model is advertised: ${embeddings_model}"
else
  ns_print_warn "Embeddings model not listed in /models yet: ${embeddings_model}"
fi

if [[ "$CHECK_ONLY" == "true" ]]; then
  ns_print_ok "Check-only mode complete"
  exit 0
fi

chat_payload_for() {
  local model="$1"
  printf '{"model":"%s","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"temperature":0}' "$model"
}

embeddings_payload_for() {
  local model="$1"
  printf '{"model":"%s","input":"ping"}' "$model"
}

ns_print_warn "Sending strong chat warmup request"
curl -fsS --max-time "$TIMEOUT_SEC" \
  -X POST "${strong_base_url%/}/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$(chat_payload_for "$strong_model")" >/dev/null
ns_print_ok "Strong chat warmup request succeeded"

ns_print_warn "Sending fast chat warmup request"
curl -fsS --max-time "$TIMEOUT_SEC" \
  -X POST "${fast_base_url%/}/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$(chat_payload_for "$fast_model")" >/dev/null
ns_print_ok "Fast chat warmup request succeeded"

ns_print_warn "Sending embeddings warmup request"
curl -fsS --max-time "$TIMEOUT_SEC" \
  -X POST "${embeddings_base_url%/}/embeddings" \
  -H "Content-Type: application/json" \
  -d "$(embeddings_payload_for "$embeddings_model")" >/dev/null
ns_print_ok "Embeddings warmup request succeeded"
