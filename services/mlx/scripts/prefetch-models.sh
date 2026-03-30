#!/usr/bin/env bash
set -euo pipefail

MLX_ENV_FILE="${MLX_ENV_FILE:-/var/lib/mlx/mlx.env}"
MLX_VENV="${MLX_VENV:-/var/lib/mlx/env}"
MLX_HOME="${MLX_HOME:-/var/lib/mlx}"

if [[ -f "$MLX_ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$MLX_ENV_FILE"
  set +a
fi

HOME="${HOME:-$MLX_HOME}"
XDG_CACHE_HOME="${XDG_CACHE_HOME:-${MLX_HOME}/cache}"
HF_HOME="${HF_HOME:-${XDG_CACHE_HOME}/huggingface}"
export HOME XDG_CACHE_HOME HF_HOME

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_PY="${THIS_DIR}/prefetch_models.py"
if [[ ! -f "$HELPER_PY" ]]; then
  HELPER_PY="${THIS_DIR}/mlx-prefetch-models.py"
fi

if [[ ! -f "$HELPER_PY" ]]; then
  echo "ERROR: helper script not found: ${HELPER_PY}" >&2
  exit 1
fi

PY_BIN="${MLX_VENV}/bin/python"
if [[ ! -x "$PY_BIN" ]]; then
  PY_BIN="$(command -v python3 2>/dev/null || true)"
fi
if [[ -z "${PY_BIN:-}" ]]; then
  echo "ERROR: no Python interpreter available for model prefetch" >&2
  exit 1
fi

declare -a forward_args=()
has_explicit_source="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|--model)
      has_explicit_source="true"
      forward_args+=("$1" "${2:-}")
      shift 2
      ;;
    *)
      forward_args+=("$1")
      shift
      ;;
  esac
done

if [[ "$has_explicit_source" != "true" ]]; then
  if [[ -n "${MLX_CONFIG_PATH:-}" ]]; then
    if [[ ${#forward_args[@]} -gt 0 ]]; then
      forward_args=(--config "$MLX_CONFIG_PATH" "${forward_args[@]}")
    else
      forward_args=(--config "$MLX_CONFIG_PATH")
    fi
  elif [[ -n "${MLX_MODEL_PATH:-}" ]]; then
    if [[ ${#forward_args[@]} -gt 0 ]]; then
      forward_args=(--model "$MLX_MODEL_PATH" "${forward_args[@]}")
    else
      forward_args=(--model "$MLX_MODEL_PATH")
    fi
  fi
fi

if [[ ${#forward_args[@]} -gt 0 ]]; then
  exec "$PY_BIN" "$HELPER_PY" "${forward_args[@]}"
fi

exec "$PY_BIN" "$HELPER_PY"
