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
WARMUP_TIMEOUT_SEC="${MLX_WARMUP_TIMEOUT_SEC:-0}"
FROM_ALIASES="false"
ALIASES_FILE="${ROOT_DIR}/.runtime/gateway/config/model_aliases.json"

declare -a MLX_MODEL_OVERRIDES=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/prewarm-mlx.sh [--env-file PATH] [--check-only] [--mlx-base-url URL] [--model MODEL] [--from-aliases] [--aliases-file PATH] [--timeout-sec N]

Checks MLX availability and optionally sends a minimal warmup generation request.
Defaults are derived from env:
  - MLX_BASE_URL (default host-native probe: http://127.0.0.1:10240/v1)
  - MLX_MODEL_PATH (default: mlx-community/gemma-2-2b-it-8bit) when MLX_CONFIG_PATH is not set

Options:
  --env-file PATH     Env file path (default: ./.env)
  --check-only        Check/report only; do not send warmup request
  --mlx-base-url URL  Explicit MLX URL (overrides MLX_BASE_URL);
                      also supported via PREWARM_MLX_BASE_URL env var.
  --model MODEL       Explicit model id/path for warmup request (repeatable)
  --from-aliases      Include all backend=mlx/local_mlx models from model_aliases.json
  --aliases-file PATH Alias config path (default: ./.runtime/gateway/config/model_aliases.json)
  --timeout-sec N     Curl timeout in seconds for each warmup request.
                    Use 0 for no timeout (default: 0)
EOF
}

add_unique_model() {
  local candidate="$1"
  [[ -n "${candidate:-}" ]] || return 0
  local existing
  for existing in "${models_to_warm[@]:-}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return 0
    fi
  done
  models_to_warm+=("$candidate")
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
      MLX_MODEL_OVERRIDES+=("${2:-}")
      shift 2
      ;;
    --from-aliases)
      FROM_ALIASES="true"
      shift
      ;;
    --aliases-file)
      ALIASES_FILE="${2:-}"
      FROM_ALIASES="true"
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
  mlx_base_url="${MLX_BASE_URL:-$(ns_env_get "$ENV_FILE" MLX_BASE_URL "")}"
  if [[ -z "${mlx_base_url:-}" ]]; then
    mlx_base_url="http://127.0.0.1:10240/v1"
  elif [[ "$mlx_base_url" == "http://host.docker.internal:10240/v1" ]]; then
    mlx_base_url="http://127.0.0.1:10240/v1"
  fi
fi
mlx_base_url="${mlx_base_url%/}"
mlx_config_path="${MLX_CONFIG_PATH:-$(ns_env_get "$ENV_FILE" MLX_CONFIG_PATH "")}"

declare -a models_to_warm=()

if [[ "${#MLX_MODEL_OVERRIDES[@]}" -gt 0 ]]; then
  for explicit_model in "${MLX_MODEL_OVERRIDES[@]}"; do
    add_unique_model "$explicit_model"
  done
elif [[ -z "${mlx_config_path:-}" ]]; then
  add_unique_model "${MLX_MODEL_PATH:-$(ns_env_get "$ENV_FILE" MLX_MODEL_PATH "mlx-community/gemma-2-2b-it-8bit")}"
fi

if [[ "$FROM_ALIASES" == "true" ]]; then
  if [[ ! -f "$ALIASES_FILE" ]]; then
    ns_die "Alias file not found: $ALIASES_FILE"
  fi
  while IFS= read -r alias_model; do
    add_unique_model "$alias_model"
  done < <(python3 - "$ALIASES_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

for alias in payload.get("aliases", {}).values():
    if not isinstance(alias, dict):
        continue
    backend = str(alias.get("backend", "")).strip().lower().replace("-", "_")
    if backend not in {"mlx", "local_mlx"} and not backend.startswith("mlx_") and not backend.startswith("local_mlx_"):
        continue
    model = str(alias.get("model", "")).strip()
    if model:
        print(model)
PY
)
fi

models_url="${mlx_base_url}/models"

ns_print_header "Prewarm MLX"
echo "MLX base URL: ${mlx_base_url}"
echo "MLX models: ${models_to_warm[*]}"

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

if [[ "${#models_to_warm[@]}" -eq 0 ]]; then
  if [[ "$CHECK_ONLY" == "true" ]]; then
    ns_print_warn "No explicit MLX warmup models resolved; config mode usually requires --model and/or --from-aliases."
    ns_print_ok "Check-only mode complete"
    exit 0
  fi
  ns_die "No MLX models resolved from options, aliases, or environment (use --model or --from-aliases when MLX_CONFIG_PATH is set)"
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

for mlx_model in "${models_to_warm[@]}"; do
  if model_present "$mlx_model" "$models_json"; then
    ns_print_ok "MLX model is advertised: ${mlx_model}"
  else
    ns_print_warn "MLX model not listed in /models yet: ${mlx_model}"
  fi
done

if [[ "$CHECK_ONLY" == "true" ]]; then
  ns_print_ok "Check-only mode complete"
  exit 0
fi

warmup_url="${mlx_base_url}/chat/completions"

for mlx_model in "${models_to_warm[@]}"; do
  warmup_payload="{\"model\":\"${mlx_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1,\"temperature\":0}"
  if [[ "$WARMUP_TIMEOUT_SEC" == "0" ]]; then
    ns_print_warn "Sending warmup request for ${mlx_model} to ${warmup_url} (timeout disabled)"
    if curl -fsS -X POST "$warmup_url" -H "Content-Type: application/json" -d "$warmup_payload" >/dev/null; then
      ns_print_ok "MLX warmup request succeeded: ${mlx_model}"
    else
      ns_die "MLX warmup request failed: ${mlx_model}"
    fi
    continue
  fi

  ns_print_warn "Sending warmup request for ${mlx_model} to ${warmup_url} (timeout ${WARMUP_TIMEOUT_SEC}s)"
  if curl -fsS --max-time "$WARMUP_TIMEOUT_SEC" -X POST "$warmup_url" -H "Content-Type: application/json" -d "$warmup_payload" >/dev/null; then
    ns_print_ok "MLX warmup request succeeded: ${mlx_model}"
  else
    ns_die "MLX warmup request failed: ${mlx_model}"
  fi
done
