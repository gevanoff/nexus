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
MLX_MODEL_PATH="${MLX_MODEL_PATH:-mlx-community/GLM-5.2-4bit}"
MLX_MODEL_TYPE="${MLX_MODEL_TYPE:-lm}"
MLX_CONFIG_PATH="${MLX_CONFIG_PATH:-}"
PREFETCH_BEFORE_START="${PREFETCH_BEFORE_START:-0}"
MLX_DISABLE_BATCHING="${MLX_DISABLE_BATCHING:-0}"
MLX_SERVER_IMPL="${MLX_SERVER_IMPL:-mlx_openai}"
MLX_DECODE_CONCURRENCY="${MLX_DECODE_CONCURRENCY:-1}"
MLX_PROMPT_CONCURRENCY="${MLX_PROMPT_CONCURRENCY:-1}"
MLX_PROMPT_CACHE_SIZE="${MLX_PROMPT_CACHE_SIZE:-1}"
MLX_MAX_TOKENS="${MLX_MAX_TOKENS:-768}"
MLX_PREFETCHER="${MLX_VENV}/bin/mlx-prefetch-models"

lowercase_value() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

case "$(lowercase_value "$PREFETCH_BEFORE_START")" in
  1 | true | yes | on)
    if [[ -x "$MLX_PREFETCHER" ]]; then
      "$MLX_PREFETCHER"
    else
      echo "WARNING: PREFETCH_BEFORE_START is enabled but prefetch helper is missing: ${MLX_PREFETCHER}" >&2
    fi
    ;;
  0 | false | no | off) ;;

  *)
    echo "ERROR: invalid PREFETCH_BEFORE_START value: ${PREFETCH_BEFORE_START}" >&2
    exit 2
    ;;
esac

case "$(lowercase_value "$MLX_SERVER_IMPL")" in
  mlx_lm | mlx-lm)
    if [[ -n "$MLX_CONFIG_PATH" ]]; then
      echo "ERROR: MLX_CONFIG_PATH is not supported with MLX_SERVER_IMPL=mlx_lm" >&2
      exit 2
    fi
    exec "${MLX_VENV}/bin/python" -m mlx_lm server \
      --model "$MLX_MODEL_PATH" \
      --host "$MLX_HOST" \
      --port "$MLX_PORT" \
      --decode-concurrency "$MLX_DECODE_CONCURRENCY" \
      --prompt-concurrency "$MLX_PROMPT_CONCURRENCY" \
      --prompt-cache-size "$MLX_PROMPT_CACHE_SIZE" \
      --max-tokens "$MLX_MAX_TOKENS"
    ;;
  mlx_openai | mlx-openai | mlx_openai_server | mlx-openai-server) ;;
  *)
    echo "ERROR: MLX_SERVER_IMPL must be mlx_openai or mlx_lm: ${MLX_SERVER_IMPL}" >&2
    exit 2
    ;;
esac

if [[ -n "$MLX_CONFIG_PATH" ]]; then
  exec "${MLX_VENV}/bin/mlx-openai-server" launch \
    --config "$MLX_CONFIG_PATH"
fi

batching_args=()
case "$(lowercase_value "$MLX_DISABLE_BATCHING")" in
  1 | true | yes | on) batching_args+=(--disable-batching) ;;
  0 | false | no | off) ;;
  *)
    echo "ERROR: invalid MLX_DISABLE_BATCHING value: ${MLX_DISABLE_BATCHING}" >&2
    exit 2
    ;;
esac

exec "${MLX_VENV}/bin/mlx-openai-server" launch \
  --model-path "$MLX_MODEL_PATH" \
  --model-type "$MLX_MODEL_TYPE" \
  --host "$MLX_HOST" \
  --port "$MLX_PORT" \
  "${batching_args[@]}"
