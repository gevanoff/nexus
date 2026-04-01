#!/usr/bin/env bash
set -euo pipefail
umask 077

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

ENV_FILE="${NEXUS_COLIMA_ENV_FILE:-${HOME}/Library/Application Support/Nexus/colima/default.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi

COLIMA_BIN="${COLIMA_BIN:-$(command -v colima || true)}"
DOCKER_BIN="${DOCKER_BIN:-$(command -v docker || true)}"
COLIMA_PROFILE="${COLIMA_PROFILE:-default}"
COLIMA_VM_TYPE="${COLIMA_VM_TYPE:-}"
COLIMA_USER_HOME="${COLIMA_USER_HOME:-${HOME:-}}"

if [[ -n "${COLIMA_USER_HOME:-}" ]]; then
  HOME="${COLIMA_USER_HOME}"
  export HOME
fi

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '%s %s\n' "$(timestamp)" "$*"
}

if [[ -z "${COLIMA_BIN:-}" ]]; then
  log "ERROR: colima executable not found in PATH"
  exit 1
fi

profile_args=()
if [[ -n "${COLIMA_PROFILE:-}" && "${COLIMA_PROFILE}" != "default" ]]; then
  profile_args+=("${COLIMA_PROFILE}")
fi

if "$COLIMA_BIN" status "${profile_args[@]}" >/dev/null 2>&1; then
  log "Colima profile '${COLIMA_PROFILE}' already running"
else
  log "Starting Colima profile '${COLIMA_PROFILE}'"
  start_cmd=("$COLIMA_BIN" start "${profile_args[@]}")
  if [[ -n "${COLIMA_VM_TYPE:-}" ]]; then
    start_cmd+=("--vm-type" "${COLIMA_VM_TYPE}")
  fi

  if ! "${start_cmd[@]}"; then
    if [[ -z "${COLIMA_VM_TYPE:-}" ]]; then
      log "Default Colima start failed; retrying with qemu fallback"
      "$COLIMA_BIN" start "${profile_args[@]}" --vm-type qemu
    else
      log "ERROR: Colima start failed with vm-type '${COLIMA_VM_TYPE}'"
      exit 1
    fi
  fi
fi

if [[ -n "${DOCKER_BIN:-}" ]]; then
  "$DOCKER_BIN" context use colima >/dev/null 2>&1 || true
fi

log "Colima launchd check completed"
