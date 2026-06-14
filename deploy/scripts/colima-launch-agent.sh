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
COLIMA_HOME="${COLIMA_HOME:-}"
COLIMA_MOUNTS="${COLIMA_MOUNTS:-}"
REPO_DIR="${REPO_DIR:-${HOME}/ai/nexus}"
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

apply_colima_home_fallback() {
  local fallback_home="${COLIMA_FALLBACK_HOME:-/ai-data/var/lib/colima}"
  [[ -n "$fallback_home" ]] || return 1
  [[ "$fallback_home" == "$COLIMA_HOME" ]] && return 1
  [[ -d "$fallback_home" ]] || return 1
  [[ -w "$fallback_home" ]] || return 1

  COLIMA_HOME="$fallback_home"
  export COLIMA_HOME
  log "Switching COLIMA_HOME fallback to '$COLIMA_HOME'"
  return 0
}

if [[ -z "${COLIMA_BIN:-}" ]]; then
  log "ERROR: colima executable not found in PATH"
  exit 1
fi

if ! wait_for_colima_home_ready; then
  log "WARNING: COLIMA_HOME path did not become fully ready in time (${COLIMA_HOME:-unset})"
fi

status_cmd=("$COLIMA_BIN" status)
start_cmd=("$COLIMA_BIN" start)
retry_cmd=("$COLIMA_BIN" start)
if [[ -n "${COLIMA_PROFILE:-}" && "${COLIMA_PROFILE}" != "default" ]]; then
  status_cmd+=("${COLIMA_PROFILE}")
  start_cmd+=("${COLIMA_PROFILE}")
  retry_cmd+=("${COLIMA_PROFILE}")
fi

if [[ -n "${COLIMA_MOUNTS:-}" ]]; then
  IFS=',' read -r -a mount_specs <<< "$COLIMA_MOUNTS"
  for mount_spec in "${mount_specs[@]:-}"; do
    [[ -n "${mount_spec:-}" ]] || continue
    start_cmd+=("--mount" "$mount_spec")
    retry_cmd+=("--mount" "$mount_spec")
  done
fi

if "${status_cmd[@]}" >/dev/null 2>&1; then
  log "Colima profile '${COLIMA_PROFILE}' already running"
else
  log "Starting Colima profile '${COLIMA_PROFILE}'"
  if [[ -n "${COLIMA_VM_TYPE:-}" ]]; then
    start_cmd+=("--vm-type" "${COLIMA_VM_TYPE}")
  fi

  if ! "${start_cmd[@]}"; then
    if [[ -z "${COLIMA_VM_TYPE:-}" ]]; then
      log "Default Colima start failed; retrying with qemu fallback"
      retry_cmd+=("--vm-type" "qemu")
      if ! "${retry_cmd[@]}"; then
        if apply_colima_home_fallback; then
          log "Retrying Colima start with fallback COLIMA_HOME and qemu"
          "${retry_cmd[@]}"
        else
          exit 1
        fi
      fi
    else
      log "ERROR: Colima start failed with vm-type '${COLIMA_VM_TYPE}'"
      if apply_colima_home_fallback; then
        log "Retrying Colima start with fallback COLIMA_HOME"
        "${start_cmd[@]}"
      else
        exit 1
      fi
    fi
  fi
fi

if [[ -n "${DOCKER_BIN:-}" ]]; then
  "$DOCKER_BIN" context use colima >/dev/null 2>&1 || true
fi

restore_ai2_services_if_needed() {
  local gateway_ok="false"
  local tts_ok="false"
  local luxtts_ok="false"
  local qwen_ok="false"

  if curl -fsS --max-time 3 "http://127.0.0.1:8801/health" >/dev/null 2>&1; then
    gateway_ok="true"
  fi
  if curl -fsS --max-time 3 "http://127.0.0.1:9940/health" >/dev/null 2>&1; then
    tts_ok="true"
  fi
  if curl -fsS --max-time 3 "http://127.0.0.1:9170/health" >/dev/null 2>&1; then
    luxtts_ok="true"
  fi
  if curl -fsS --max-time 3 "http://127.0.0.1:9175/health" >/dev/null 2>&1; then
    qwen_ok="true"
  fi

  if [[ "$gateway_ok" == "true" && "$tts_ok" == "true" && "$luxtts_ok" == "true" && "$qwen_ok" == "true" ]]; then
    return 0
  fi

  if [[ ! -x "${REPO_DIR}/deploy/scripts/restart-ai2-services.sh" ]]; then
    log "WARNING: ai2 service restore helper not found at ${REPO_DIR}/deploy/scripts/restart-ai2-services.sh"
    return 1
  fi

  log "Restoring ai2 services because one or more endpoints are down: gateway=${gateway_ok} tts=${tts_ok} luxtts=${luxtts_ok} qwen3-tts=${qwen_ok}"
  if ! "${REPO_DIR}/deploy/scripts/restart-ai2-services.sh" --env-file "${COLIMA_USER_HOME:-${HOME:-}}/ai/nexus/.env"; then
    log "ERROR: ai2 service restore failed"
    return 1
  fi

  return 0
}

if [[ "${COLIMA_PROFILE:-default}" == "default" ]]; then
  restore_ai2_services_if_needed || true
fi

log "Colima launchd check completed"
