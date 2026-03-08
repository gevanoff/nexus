#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${PERSONAPLEX_DATA_DIR:-/data}"
APP_DIR="${PERSONAPLEX_APP_DIR:-${DATA_DIR}/app}"
REPO_URL="${PERSONAPLEX_REPO_URL:-https://github.com/NVIDIA/personaplex}"
REPO_REF="${PERSONAPLEX_REPO_REF:-}"
UPDATE_ON_START="${PERSONAPLEX_UPDATE_ON_START:-false}"
REQ_HASH_FILE="${DATA_DIR}/.personaplex-requirements.sha256"

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
    if [[ "${req_hash}" != "${prev_hash}" ]]; then
      pip install --no-cache-dir -r "${APP_DIR}/requirements.txt" || true
      printf '%s' "${req_hash}" > "${REQ_HASH_FILE}"
    fi
  fi
fi

export PERSONAPLEX_WORKDIR="${PERSONAPLEX_WORKDIR:-${APP_DIR}}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PERSONAPLEX_PORT:-9160}"