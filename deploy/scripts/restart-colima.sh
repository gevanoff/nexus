#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

if [[ "$(ns_detect_platform)" != "macos" ]]; then
  ns_die "This helper is macOS-only"
fi

PROFILE="default"
TARGET_USER=""
TARGET_HOME=""
TARGET_COLIMA_HOME=""
TARGET_COLIMA_USER_HOME="${COLIMA_USER_HOME:-}"
MANAGED_COLIMA_HOME=""
LABEL=""
TIMEOUT_SEC="75"
COLIMA_RUNTIME_ROOT="${COLIMA_RUNTIME_ROOT:-/var/lib/nexus-colima}"
declare -a EXTRA_MOUNTS=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restart-colima.sh [--profile NAME] [--user USER] [--home PATH] [--colima-home PATH] [--colima-user-home PATH] [--runtime-root PATH] [--label LABEL] [--timeout-sec N] [--mount PATH[:MODE]]

Restart the Colima LaunchDaemon and wait for Docker to become reachable.

Options:
  --profile NAME     Colima profile name (default: default)
  --user USER        User account that owns/runs Colima (default: current user)
  --home PATH        Home directory for the selected user (default: detected from dscl/$HOME)
  --colima-home PATH Colima state root to export as COLIMA_HOME when checking Docker
  --colima-user-home PATH
                     Home/config root to export as COLIMA_USER_HOME and HOME when checking Docker
  --runtime-root PATH
                     Root-owned asset directory used by install-colima-launchd.sh (default: /var/lib/nexus-colima)
  --label LABEL      LaunchDaemon label (default: com.nexus.colima.<user>.<profile>)
  --timeout-sec N    Wait time for Docker daemon health (default: 75)
  --mount PATH[:MODE]
                     One-off explicit Colima host mount for this restart (repeatable). MODE defaults to w.
EOF
}

sanitize_profile() {
  printf '%s' "${1:-default}" | tr -c 'A-Za-z0-9._-' '_'
}

resolve_target_user() {
  if [[ -n "${TARGET_USER:-}" ]]; then
    printf '%s\n' "$TARGET_USER"
    return 0
  fi

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
      printf '%s\n' "$SUDO_USER"
      return 0
    fi
    ns_die "When running as root, pass --user USER (or invoke the script via sudo from your normal account)"
  fi

  id -un
}

resolve_home_for_user() {
  local user_name="$1"
  local home_dir=""

  if [[ -n "${TARGET_HOME:-}" ]]; then
    printf '%s\n' "$TARGET_HOME"
    return 0
  fi

  if [[ "${user_name}" == "$(id -un)" && -n "${HOME:-}" ]]; then
    printf '%s\n' "$HOME"
    return 0
  fi

  home_dir="$(dscl . -read "/Users/${user_name}" NFSHomeDirectory 2>/dev/null | awk '{print $2}' | tail -n 1 || true)"
  if [[ -n "${home_dir:-}" ]]; then
    printf '%s\n' "$home_dir"
    return 0
  fi

  ns_die "Could not determine home directory for user '${user_name}'"
}

run_docker_for_target() {
  if [[ -z "${DOCKER_BIN:-}" ]]; then
    return 1
  fi

  local docker_home="${TARGET_COLIMA_USER_HOME:-${TARGET_HOME}}"

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    sudo -H -u "${TARGET_USER}" HOME="${docker_home}" COLIMA_HOME="${TARGET_COLIMA_HOME:-}" DOCKER_CONTEXT="${DOCKER_CONTEXT}" "${DOCKER_BIN}" "$@"
  else
    HOME="${docker_home}" COLIMA_HOME="${TARGET_COLIMA_HOME:-}" DOCKER_CONTEXT="${DOCKER_CONTEXT}" "${DOCKER_BIN}" "$@"
  fi
}

persist_docker_context_for_target() {
  [[ -n "${DOCKER_BIN:-}" ]] || return 1
  local docker_home="${TARGET_COLIMA_USER_HOME:-${TARGET_HOME}}"

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    sudo -H -u "${TARGET_USER}" env -u DOCKER_CONTEXT HOME="${docker_home}" COLIMA_HOME="${TARGET_COLIMA_HOME:-}" \
      "${DOCKER_BIN}" context use "${DOCKER_CONTEXT}"
  else
    env -u DOCKER_CONTEXT HOME="${docker_home}" COLIMA_HOME="${TARGET_COLIMA_HOME:-}" \
      "${DOCKER_BIN}" context use "${DOCKER_CONTEXT}"
  fi
}

wait_for_docker_as_target() {
  local timeout_sec="$1"
  local elapsed=0

  while [[ "$elapsed" -lt "$timeout_sec" ]]; do
    if run_docker_for_target info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  return 1
}

normalize_mount_spec() {
  local raw="$1"
  local path_part="$raw"
  local mode_part="w"

  if [[ "$raw" == *:* ]]; then
    path_part="${raw%%:*}"
    mode_part="${raw##*:}"
  fi

  [[ -n "$path_part" ]] || return 1
  if [[ ! -e "$path_part" ]]; then
    ns_print_warn "Skipping Colima mount because the host path does not exist: $path_part"
    return 1
  fi

  local physical_path=""
  physical_path="$(cd "$path_part" 2>/dev/null && pwd -P)" || physical_path="$(cd "$(dirname "$path_part")" && pwd -P)/$(basename "$path_part")"
  [[ -n "$physical_path" ]] || return 1
  printf '%s:%s\n' "$physical_path" "$mode_part"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:-}"
      shift 2
      ;;
    --user)
      TARGET_USER="${2:-}"
      shift 2
      ;;
    --home)
      TARGET_HOME="${2:-}"
      shift 2
      ;;
    --colima-home)
      TARGET_COLIMA_HOME="${2:-}"
      shift 2
      ;;
    --colima-user-home)
      TARGET_COLIMA_USER_HOME="${2:-}"
      shift 2
      ;;
    --runtime-root)
      COLIMA_RUNTIME_ROOT="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
      shift 2
      ;;
    --timeout-sec)
      TIMEOUT_SEC="${2:-}"
      shift 2
      ;;
    --mount)
      EXTRA_MOUNTS+=("${2:-}")
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

[[ -n "${PROFILE:-}" ]] || ns_die "--profile must not be empty"
[[ "$TIMEOUT_SEC" =~ ^[0-9]+$ ]] || ns_die "--timeout-sec must be an integer"
[[ "${COLIMA_RUNTIME_ROOT}" == /* ]] || ns_die "--runtime-root must be an absolute path"

ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd dscl "dscl" || exit 1
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  ns_require_cmd sudo "sudo" || exit 1
fi

TARGET_USER="$(resolve_target_user)"
[[ "${TARGET_USER}" != "root" ]] || ns_die "Colima target user must not be root"
TARGET_HOME="$(resolve_home_for_user "$TARGET_USER")"
SANITIZED_PROFILE="$(sanitize_profile "$PROFILE")"
SANITIZED_USER="$(sanitize_profile "$TARGET_USER")"
if [[ -z "${LABEL:-}" ]]; then
  LABEL="com.nexus.colima.${SANITIZED_USER}.${SANITIZED_PROFILE}"
fi

PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
ENV_FILE="${TARGET_HOME}/.colima/${SANITIZED_USER}-${SANITIZED_PROFILE}.env"
if [[ ! -f "$ENV_FILE" ]]; then
  ENV_FILE="${COLIMA_RUNTIME_ROOT}/${SANITIZED_USER}-${SANITIZED_PROFILE}.env"
fi

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  MANAGED_COLIMA_HOME="${COLIMA_HOME:-}"
fi

DOCKER_BIN="${DOCKER_BIN:-$(command -v docker || true)}"
if [[ -z "${TARGET_COLIMA_HOME:-}" ]]; then
  TARGET_COLIMA_HOME="${MANAGED_COLIMA_HOME:-${TARGET_HOME}/.colima}"
fi
if [[ -z "${TARGET_COLIMA_USER_HOME:-}" ]]; then
  TARGET_COLIMA_USER_HOME="${COLIMA_USER_HOME:-$TARGET_HOME}"
fi
COLIMA_PROFILE="$PROFILE"
export COLIMA_PROFILE
ns_activate_colima_docker_context

if [[ ${#EXTRA_MOUNTS[@]} -gt 0 ]]; then
  normalized_mounts=()
  existing_mounts_csv="${COLIMA_MOUNTS:-}"
  if [[ -n "${existing_mounts_csv:-}" ]]; then
    IFS=',' read -r -a existing_mounts <<<"$existing_mounts_csv"
    for mount_spec in "${existing_mounts[@]:-}"; do
      normalized="$(normalize_mount_spec "$mount_spec" || true)"
      [[ -n "${normalized:-}" ]] && normalized_mounts+=("$normalized")
    done
  fi
  for mount_spec in "${EXTRA_MOUNTS[@]:-}"; do
    normalized="$(normalize_mount_spec "$mount_spec" || true)"
    if [[ -n "${normalized:-}" ]]; then
      duplicate="false"
      for existing in "${normalized_mounts[@]:-}"; do
        if [[ "$existing" == "$normalized" ]]; then
          duplicate="true"
          break
        fi
      done
      if [[ "$duplicate" != "true" ]]; then
        normalized_mounts+=("$normalized")
      fi
    fi
  done
  if [[ ${#normalized_mounts[@]} -gt 0 ]]; then
    printf -v merged_mounts_csv '%s,' "${normalized_mounts[@]}"
    export COLIMA_MOUNTS="${merged_mounts_csv%,}"
    ns_print_warn "Applying explicit Colima mounts for this restart: ${COLIMA_MOUNTS}"
  fi
fi

ns_print_header "Restarting Colima LaunchDaemon"

if sudo launchctl print "system/${LABEL}" >/dev/null 2>&1; then
  ns_print_ok "LaunchDaemon is loaded: ${LABEL}"
else
  if [[ ! -f "$PLIST_PATH" ]]; then
    ns_print_error "Colima LaunchDaemon not found: ${PLIST_PATH}"
    ns_print_warn "Install it first:"
    ns_print_warn "  ./deploy/scripts/install-colima-launchd.sh --profile ${PROFILE} --user ${TARGET_USER} --runtime-root ${COLIMA_RUNTIME_ROOT}"
    exit 1
  fi
  ns_print_warn "LaunchDaemon is not loaded; bootstrapping ${PLIST_PATH}"
  sudo launchctl bootstrap system "$PLIST_PATH"
fi

sudo launchctl kickstart -k "system/${LABEL}"

ns_print_header "Waiting for Docker via Colima"
persist_docker_context_for_target >/dev/null 2>&1 || true
if wait_for_docker_as_target "$TIMEOUT_SEC"; then
  ns_print_ok "Docker daemon is reachable via Colima"
  exit 0
fi

ns_print_error "Docker daemon did not become reachable within ${TIMEOUT_SEC}s"
sudo launchctl print "system/${LABEL}" || true
exit 1
