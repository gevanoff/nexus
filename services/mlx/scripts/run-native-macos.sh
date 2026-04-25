#!/usr/bin/env bash
set -euo pipefail

MLX_ENV_FILE="${MLX_ENV_FILE:-/var/lib/mlx/mlx.env}"
MLX_VENV="${MLX_VENV:-/var/lib/mlx/env}"

if [[ -f "$MLX_ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$MLX_ENV_FILE"
  set +a
fi

MLX_HOST="${MLX_HOST:-127.0.0.1}"
MLX_PORT="${MLX_PORT:-10240}"
MLX_MODEL_PATH="${MLX_MODEL_PATH:-mlx-community/Qwen3-30B-A3B-4bit}"
MLX_MODEL_TYPE="${MLX_MODEL_TYPE:-lm}"
MLX_CONFIG_PATH="${MLX_CONFIG_PATH:-}"
PREFETCH_BEFORE_START="${PREFETCH_BEFORE_START:-0}"
MLX_PREFETCHER="${MLX_VENV}/bin/mlx-prefetch-models"

lowercase_value() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

case "$(lowercase_value "$PREFETCH_BEFORE_START")" in
  1|true|yes|on)
    if [[ -x "$MLX_PREFETCHER" ]]; then
      "$MLX_PREFETCHER"
    else
      echo "WARNING: PREFETCH_BEFORE_START is enabled but prefetch helper is missing: ${MLX_PREFETCHER}" >&2
    fi
    ;;
  0|false|no|off)
    ;;
  *)
    echo "ERROR: invalid PREFETCH_BEFORE_START value: ${PREFETCH_BEFORE_START}" >&2
    exit 2
    ;;
esac

if [[ -n "$MLX_CONFIG_PATH" ]]; then
  exec "${MLX_VENV}/bin/mlx-openai-server" launch \
    --config "$MLX_CONFIG_PATH"
fi

exec "${MLX_VENV}/bin/mlx-openai-server" launch \
  --model-path "$MLX_MODEL_PATH" \
  --model-type "$MLX_MODEL_TYPE" \
  --host "$MLX_HOST" \
  --port "$MLX_PORT"
