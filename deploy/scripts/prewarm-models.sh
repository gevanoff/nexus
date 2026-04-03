#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
VLLM_ARGS=()
MLX_ARGS=()
CHECK_ONLY="false"
ALIASES_FILE="${ROOT_DIR}/.runtime/gateway/config/model_aliases.json"
USE_ALIAS_MODELS="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/prewarm-models.sh [--env-file PATH] [--check-only]
                                        [--strong-base-url URL] [--fast-base-url URL] [--embeddings-base-url URL]
                                        [--mlx-base-url URL] [--model MODEL] [--from-aliases] [--aliases-file PATH]

Deprecated compatibility wrapper that warms the split LLM topology:
  1) vLLM endpoints (strong, fast, embeddings)
  2) MLX models when MLX is configured

Legacy flags kept for compatibility:
  --external-ollama
  --ollama-base-url URL
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      VLLM_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --check-only)
      CHECK_ONLY="true"
      VLLM_ARGS+=("$1")
      shift
      ;;
    --strong-base-url|--fast-base-url|--embeddings-base-url)
      if [[ $# -lt 2 ]]; then
        echo "missing value for $1" >&2
        exit 2
      fi
      VLLM_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --timeout-sec)
      if [[ $# -lt 2 ]]; then
        echo "missing value for $1" >&2
        exit 2
      fi
      VLLM_ARGS+=("$1" "${2:-}")
      MLX_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --mlx-base-url|--model)
      if [[ $# -lt 2 ]]; then
        echo "missing value for $1" >&2
        exit 2
      fi
      MLX_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --from-aliases)
      USE_ALIAS_MODELS="true"
      shift
      ;;
    --aliases-file)
      if [[ $# -lt 2 ]]; then
        echo "missing value for $1" >&2
        exit 2
      fi
      ALIASES_FILE="${2:-}"
      USE_ALIAS_MODELS="true"
      shift 2
      ;;
    --external-ollama)
      shift
      ;;
    --ollama-base-url)
      if [[ $# -lt 2 ]]; then
        echo "missing value for --ollama-base-url" >&2
        exit 2
      fi
      VLLM_ARGS+=(--strong-base-url "${2:-}")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

echo "deploy/scripts/prewarm-models.sh is deprecated; warming vLLM first, then MLX when configured." >&2
bash "$ROOT_DIR/deploy/scripts/prewarm-vllm.sh" "${VLLM_ARGS[@]}"

mlx_base_url="${MLX_BASE_URL:-$(ns_env_get "$ENV_FILE" MLX_BASE_URL "")}"

if [[ -z "${mlx_base_url:-}" && ! -f "$ALIASES_FILE" && "${#MLX_ARGS[@]}" -eq 0 ]]; then
  echo "deploy/scripts/prewarm-models.sh: MLX not configured; skipping MLX warmup." >&2
  exit 0
fi

MLX_RUN_ARGS=(--env-file "$ENV_FILE")
if [[ "$CHECK_ONLY" == "true" ]]; then
  MLX_RUN_ARGS+=(--check-only)
fi
if [[ "${#MLX_ARGS[@]}" -gt 0 ]]; then
  MLX_RUN_ARGS+=("${MLX_ARGS[@]}")
fi
if [[ "$USE_ALIAS_MODELS" == "true" || -f "$ALIASES_FILE" ]]; then
  MLX_RUN_ARGS+=(--from-aliases --aliases-file "$ALIASES_FILE")
fi

exec bash "$ROOT_DIR/deploy/scripts/prewarm-mlx.sh" "${MLX_RUN_ARGS[@]}"
