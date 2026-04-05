#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${SKYREELS_DATA_DIR:-/data}"
APP_DIR="${SKYREELS_APP_DIR:-${DATA_DIR}/app}"
REPO_URL="${SKYREELS_REPO_URL:-https://github.com/SkyworkAI/SkyReels-V2}"
REPO_REF="${SKYREELS_REPO_REF:-}"
UPDATE_ON_START="${SKYREELS_UPDATE_ON_START:-false}"
REQ_HASH_FILE="${DATA_DIR}/.skyreels-requirements.sha256"
FALLBACK_REQ_FILE="/app/runtime-requirements.txt"

skyreels_runtime_ready() {
  python3 - <<'PY' >/dev/null 2>&1
import importlib
modules = ("torch", "diffusers", "transformers", "decord", "einops", "moviepy", "safetensors")
for name in modules:
    importlib.import_module(name)
PY
}

skyreels_install_runtime() {
  local req_file="$1"
  if python3 -m pip install --no-cache-dir -r "$req_file"; then
    return 0
  fi

  if [[ "$req_file" == "$FALLBACK_REQ_FILE" ]]; then
    return 1
  fi
  if [[ ! -f "$FALLBACK_REQ_FILE" ]]; then
    return 1
  fi

  echo "SkyReels upstream requirements install failed; falling back to curated runtime requirements." >&2
  python3 -m pip install --no-cache-dir -r "$FALLBACK_REQ_FILE"
}

mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs"

if [[ -n "${REPO_URL}" ]]; then
  if [[ ! -d "${APP_DIR}/.git" ]]; then
    git clone "${REPO_URL}" "${APP_DIR}" || true
  elif [[ "${UPDATE_ON_START,,}" == "true" ]]; then
    git -C "${APP_DIR}" fetch --all --tags || true
    git -C "${APP_DIR}" pull --ff-only || true
  fi

  if [[ -n "${REPO_REF}" && -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" fetch --all --tags || true
    git -C "${APP_DIR}" checkout "${REPO_REF}" || true
  fi

  if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    req_hash="$(sha256sum "${APP_DIR}/requirements.txt" | awk '{print $1}')"
    prev_hash="$(cat "${REQ_HASH_FILE}" 2>/dev/null || true)"
    if [[ "${req_hash}" != "${prev_hash}" ]] || ! skyreels_runtime_ready; then
      skyreels_install_runtime "${APP_DIR}/requirements.txt"
      printf '%s' "${req_hash}" > "${REQ_HASH_FILE}"
    fi
  fi
fi

export SKYREELS_WORKDIR="${SKYREELS_WORKDIR:-${APP_DIR}}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${SKYREELS_PORT:-9180}"
