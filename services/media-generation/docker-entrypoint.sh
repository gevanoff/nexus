#!/usr/bin/env bash
set -euo pipefail

ENGINE="${NEXUS_MEDIA_ENGINE:?NEXUS_MEDIA_ENGINE must be set}"
DATA_DIR="${MEDIA_DATA_DIR:-/data}"
UPSTREAM_DIR="${MEDIA_UPSTREAM_DIR:-${DATA_DIR}/app}"
OUTPUT_ROOT="${MEDIA_OUTPUT_ROOT:-${DATA_DIR}/outputs}"
UPDATE_ON_START="${MEDIA_UPDATE_ON_START:-false}"

mkdir -p "${DATA_DIR}" "${OUTPUT_ROOT}" "${DATA_DIR}/models" "${DATA_DIR}/cache"

clone_or_update() {
  local repo_url="$1"
  local repo_ref="${2:-}"
  if [[ ! -d "${UPSTREAM_DIR}/.git" ]]; then
    git clone --filter=blob:none "${repo_url}" "${UPSTREAM_DIR}"
  elif [[ "${UPDATE_ON_START,,}" == "true" ]]; then
    git -C "${UPSTREAM_DIR}" fetch --all --tags
    git -C "${UPSTREAM_DIR}" pull --ff-only
  fi
  if [[ -n "${repo_ref}" ]]; then
    git -C "${UPSTREAM_DIR}" fetch --all --tags
    git -C "${UPSTREAM_DIR}" checkout "${repo_ref}"
  fi
}

uv_sync_runtime() {
  local sync_args=("$@")
  if [[ -f uv.lock ]]; then
    if uv sync --frozen "${sync_args[@]}"; then
      return 0
    fi
    echo "Frozen uv sync failed; retrying without --frozen for runtime-cloned upstream" >&2
  else
    echo "No uv.lock found; running non-frozen uv sync for runtime-cloned upstream" >&2
  fi
  uv sync "${sync_args[@]}"
}

wait_for_http() {
  local url="$1"
  local attempts="${2:-120}"
  local delay="${3:-2}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${delay}"
  done
  return 1
}

case "${ENGINE}" in
  ltx)
    clone_or_update \
      "${LTX_REPO_URL:-https://github.com/Lightricks/LTX-2.git}" \
      "${LTX_REPO_REF:-}"
    cd "${UPSTREAM_DIR}"
    read -r -a ltx_uv_sync_args <<< "${LTX_UV_SYNC_ARGS:---extra xformers}"
    uv_sync_runtime "${ltx_uv_sync_args[@]}"
    export MEDIA_RUNNER_PYTHON="${UPSTREAM_DIR}/.venv/bin/python"
    export MEDIA_UPSTREAM_DIR="${UPSTREAM_DIR}"
    cd /app
    exec uvicorn app.video_main:app \
      --host 0.0.0.0 \
      --port "${MEDIA_PORT:-9180}"
    ;;

  hunyuan)
    clone_or_update \
      "${HUNYUAN_REPO_URL:-https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5.git}" \
      "${HUNYUAN_REPO_REF:-}"
    cd "${UPSTREAM_DIR}"
    if [[ ! -x "${UPSTREAM_DIR}/.venv/bin/python" ]]; then
      uv venv --python "${HUNYUAN_PYTHON_VERSION:-3.10}" "${UPSTREAM_DIR}/.venv"
    fi
    if [[ -f requirements.txt ]]; then
      uv pip install --python "${UPSTREAM_DIR}/.venv/bin/python" -r requirements.txt
    fi
    export MEDIA_RUNNER_PYTHON="${UPSTREAM_DIR}/.venv/bin/python"
    export MEDIA_UPSTREAM_DIR="${UPSTREAM_DIR}"
    cd /app
    exec uvicorn app.video_main:app \
      --host 0.0.0.0 \
      --port "${MEDIA_PORT:-9185}"
    ;;

  ace_step)
    clone_or_update \
      "${ACE_STEP_REPO_URL:-https://github.com/ace-step/ACE-Step-1.5.git}" \
      "${ACE_STEP_REPO_REF:-}"
    cd "${UPSTREAM_DIR}"
    read -r -a ace_step_uv_sync_args <<< "${ACE_STEP_UV_SYNC_ARGS:-}"
    uv_sync_runtime "${ace_step_uv_sync_args[@]}"

    export ACESTEP_API_HOST="${ACESTEP_API_HOST:-127.0.0.1}"
    export ACESTEP_API_PORT="${ACESTEP_API_PORT:-8001}"
    export ACESTEP_CONFIG_PATH="${ACESTEP_CONFIG_PATH:-${ACE_STEP_DIT_MODEL:-acestep-v15-xl-sft}}"
    export ACESTEP_LM_MODEL_PATH="${ACESTEP_LM_MODEL_PATH:-${ACE_STEP_LM_MODEL:-acestep-5Hz-lm-4B}}"
    export ACESTEP_LM_BACKEND="${ACESTEP_LM_BACKEND:-vllm}"

    uv run acestep-api &
    upstream_pid=$!
    trap 'kill "${upstream_pid}" 2>/dev/null || true' EXIT INT TERM
    if ! wait_for_http "http://${ACESTEP_API_HOST}:${ACESTEP_API_PORT}/health" \
      "${ACE_STEP_STARTUP_ATTEMPTS:-180}" \
      "${ACE_STEP_STARTUP_DELAY_SEC:-2}"; then
      echo "ACE-Step API failed to become healthy" >&2
      exit 1
    fi
    export ACE_STEP_UPSTREAM_BASE_URL="http://${ACESTEP_API_HOST}:${ACESTEP_API_PORT}"
    cd /app
    uvicorn app.music_main:app \
      --host 0.0.0.0 \
      --port "${MEDIA_PORT:-9195}" &
    shim_pid=$!
    wait -n "${upstream_pid}" "${shim_pid}"
    status=$?
    kill "${upstream_pid}" "${shim_pid}" 2>/dev/null || true
    wait "${upstream_pid}" "${shim_pid}" 2>/dev/null || true
    exit "${status}"
    ;;

  *)
    echo "Unsupported NEXUS_MEDIA_ENGINE: ${ENGINE}" >&2
    exit 2
    ;;
esac
