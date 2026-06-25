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
COLIMA_CPU="${COLIMA_CPU:-}"
COLIMA_MEMORY="${COLIMA_MEMORY:-}"
COLIMA_DISK="${COLIMA_DISK:-}"
COLIMA_USER_HOME="${COLIMA_USER_HOME:-${HOME:-}}"
COLIMA_HOME="${COLIMA_HOME:-}"
COLIMA_MOUNTS="${COLIMA_MOUNTS:-}"
if [[ "$COLIMA_PROFILE" == "default" ]]; then
  DOCKER_CONTEXT="colima"
else
  DOCKER_CONTEXT="colima-${COLIMA_PROFILE}"
fi
export DOCKER_CONTEXT
if [[ -n "${COLIMA_HOME:-}" ]]; then
  export COLIMA_HOME
fi

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

resolve_link_target() {
  local link_path="$1"
  local target=""

  target="$(readlink "$link_path" 2>/dev/null || true)"
  [[ -n "$target" ]] || return 1
  if [[ "$target" != /* ]]; then
    target="$(cd "$(dirname "$link_path")" && pwd -P)/$target"
  fi
  printf '%s\n' "$target"
}

wait_for_colima_home_ready() {
  local wait_sec="${COLIMA_HOME_WAIT_SEC:-120}"
  local elapsed=0
  local lima_target=""

  [[ "$wait_sec" =~ ^[0-9]+$ ]] || wait_sec=120
  [[ -n "${COLIMA_HOME:-}" ]] || return 0

  while [[ "$elapsed" -lt "$wait_sec" ]]; do
    if [[ -d "$COLIMA_HOME" && -w "$COLIMA_HOME" ]]; then
      if [[ -L "$COLIMA_HOME/_lima" ]]; then
        lima_target="$(resolve_link_target "$COLIMA_HOME/_lima" || true)"
        if [[ -n "$lima_target" && -d "$lima_target" && -x "$lima_target" ]]; then
          return 0
        fi
      else
        return 0
      fi
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  return 1
}

wait_for_colima_mounts_ready() {
  local wait_sec="${COLIMA_MOUNTS_WAIT_SEC:-300}"
  local elapsed=0
  local mount_spec=""
  local mount_path=""
  local all_ready="true"

  [[ "$wait_sec" =~ ^[0-9]+$ ]] || wait_sec=300
  [[ -n "${COLIMA_MOUNTS:-}" ]] || return 0

  while [[ "$elapsed" -lt "$wait_sec" ]]; do
    all_ready="true"
    IFS=',' read -r -a mount_specs <<<"$COLIMA_MOUNTS"
    for mount_spec in "${mount_specs[@]:-}"; do
      [[ -n "${mount_spec:-}" ]] || continue
      mount_path="${mount_spec%:*}"
      if [[ ! -d "$mount_path" || ! -x "$mount_path" ]]; then
        all_ready="false"
        break
      fi
    done
    [[ "$all_ready" == "true" ]] && return 0
    sleep 2
    elapsed=$((elapsed + 2))
  done

  return 1
}

if [[ -z "${COLIMA_BIN:-}" ]]; then
  log "ERROR: colima executable not found in PATH"
  exit 1
fi

if ! wait_for_colima_home_ready; then
  log "WARNING: COLIMA_HOME path did not become fully ready in time (${COLIMA_HOME:-unset})"
fi

status_cmd=("$COLIMA_BIN" status --profile "$COLIMA_PROFILE")
start_cmd=("$COLIMA_BIN" start --profile "$COLIMA_PROFILE")
stop_cmd=("$COLIMA_BIN" stop --force --profile "$COLIMA_PROFILE")

if [[ -n "${COLIMA_CPU:-}" ]]; then
  start_cmd+=("--cpu" "$COLIMA_CPU")
fi
if [[ -n "${COLIMA_MEMORY:-}" ]]; then
  start_cmd+=("--memory" "$COLIMA_MEMORY")
fi
if [[ -n "${COLIMA_DISK:-}" ]]; then
  start_cmd+=("--disk" "$COLIMA_DISK")
fi

if [[ -n "${COLIMA_MOUNTS:-}" ]]; then
  IFS=',' read -r -a mount_specs <<<"$COLIMA_MOUNTS"
  for mount_spec in "${mount_specs[@]:-}"; do
    [[ -n "${mount_spec:-}" ]] || continue
    start_cmd+=("--mount" "$mount_spec")
  done
fi

if "${status_cmd[@]}" >/dev/null 2>&1; then
  log "Colima profile '${COLIMA_PROFILE}' already running"
else
  if ! wait_for_colima_mounts_ready; then
    log "WARNING: configured Colima mounts did not become ready in time (${COLIMA_MOUNTS})"
    exit 75
  fi
  log "Starting Colima profile '${COLIMA_PROFILE}'"
  # A failed VZ start can leave Lima sockets or disk attachment state behind.
  # Reset only this stopped profile before starting it; this also prevents the
  # launchd interval from accumulating stale usernet helpers.
  "${stop_cmd[@]}" >/dev/null 2>&1 || true
  if [[ -n "${COLIMA_VM_TYPE:-}" ]]; then
    start_cmd+=("--vm-type" "${COLIMA_VM_TYPE}")
  fi

  if ! "${start_cmd[@]}"; then
    "${stop_cmd[@]}" >/dev/null 2>&1 || true
    log "ERROR: Colima profile '${COLIMA_PROFILE}' failed to start with its configured settings"
    exit 1
  fi
fi

if [[ -n "${DOCKER_BIN:-}" ]]; then
  env -u DOCKER_CONTEXT "$DOCKER_BIN" context use "$DOCKER_CONTEXT" >/dev/null 2>&1 || true
fi

log "Colima launchd check completed"
