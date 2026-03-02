#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
CHECK_ONLY="false"
MLX_BASE_URL_OVERRIDE="${PREWARM_MLX_BASE_URL:-}"
MLX_MODEL_OVERRIDE=""
WARMUP_TIMEOUT_SEC="${MLX_WARMUP_TIMEOUT_SEC:-180}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/prewarm-mlx.sh [--env-file PATH] [--check-only] [--mlx-base-url URL] [--model MODEL] [--timeout-sec N]

Checks MLX availability and optionally sends a minimal warmup generation request.
Defaults are derived from env:
  - MLX_BASE_URL (default: http://mlx:10240/v1)
  - MLX_MODEL_PATH (default: mlx-community/gemma-2-2b-it-8bit)

Options:
  --env-file PATH     Env file path (default: ./.env)
  --check-only        Check/report only; do not send warmup request
  --mlx-base-url URL  Explicit MLX URL (overrides MLX_BASE_URL);
                      also supported via PREWARM_MLX_BASE_URL env var.
  --model MODEL       Explicit model id/path for warmup request
  --timeout-sec N     Curl timeout in seconds for warmup request (default: 180)
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
    --mlx-base-url)
      MLX_BASE_URL_OVERRIDE="${2:-}"
      shift 2
      ;;
    --model)
      MLX_MODEL_OVERRIDE="${2:-}"
      shift 2
      ;;
    --timeout-sec)
      WARMUP_TIMEOUT_SEC="${2:-}"
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

if [[ ! "$WARMUP_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  ns_die "Invalid --timeout-sec value: ${WARMUP_TIMEOUT_SEC}"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

ns_require_cmd curl || exit 1
ns_require_cmd python3 || exit 1

if [[ -n "${MLX_BASE_URL_OVERRIDE:-}" ]]; then
  mlx_base_url="$MLX_BASE_URL_OVERRIDE"
else
  mlx_base_url="${MLX_BASE_URL:-$(ns_env_get "$ENV_FILE" MLX_BASE_URL "http://mlx:10240/v1")}"
fi
mlx_base_url="${mlx_base_url%/}"

if [[ -n "${MLX_MODEL_OVERRIDE:-}" ]]; then
  mlx_model="$MLX_MODEL_OVERRIDE"
else
  mlx_model="${MLX_MODEL_PATH:-$(ns_env_get "$ENV_FILE" MLX_MODEL_PATH "mlx-community/gemma-2-2b-it-8bit")}"
fi

models_url="${mlx_base_url}/models"

ns_print_header "Prewarm MLX"
echo "MLX base URL: ${mlx_base_url}"
echo "MLX model: ${mlx_model}"

models_json="$(curl -fsS "$models_url" 2>/dev/null || true)"
if [[ -z "$models_json" ]]; then
  fallback_base_url="${mlx_base_url/host.docker.internal/127.0.0.1}"
  if [[ "$fallback_base_url" != "$mlx_base_url" ]]; then
    ns_print_warn "Retrying MLX endpoint using host-local fallback: ${fallback_base_url}"
    fallback_models_url="${fallback_base_url}/models"
    models_json="$(curl -fsS "$fallback_models_url" 2>/dev/null || true)"
    if [[ -n "$models_json" ]]; then
      mlx_base_url="$fallback_base_url"
      models_url="$fallback_models_url"
    fi
  fi
fi

if [[ -z "$models_json" ]]; then
  ns_die "Could not reach MLX models endpoint at ${models_url}"
fi

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
    mid = item.get("id", "")
    if mid == target:
      sys.exit(0)

sys.exit(1)
PY
}

if model_present "$mlx_model" "$models_json"; then
  ns_print_ok "MLX model is advertised: ${mlx_model}"
else
  ns_print_warn "MLX model not listed in /models yet: ${mlx_model}"
fi

if [[ "$CHECK_ONLY" == "true" ]]; then
  ns_print_ok "Check-only mode complete"
  exit 0
fi

warmup_url="${mlx_base_url}/chat/completions"
warmup_payload="{\"model\":\"${mlx_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1,\"temperature\":0}"

ns_print_warn "Sending warmup request to ${warmup_url} (timeout ${WARMUP_TIMEOUT_SEC}s)"
if curl -fsS --max-time "$WARMUP_TIMEOUT_SEC" -X POST "$warmup_url" -H "Content-Type: application/json" -d "$warmup_payload" >/dev/null; then
  ns_print_ok "MLX warmup request succeeded"
else
  ns_die "MLX warmup request failed"
fi
