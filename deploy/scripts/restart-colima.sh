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
LABEL=""
TIMEOUT_SEC="75"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restart-colima.sh [--profile NAME] [--user USER] [--home PATH] [--label LABEL] [--timeout-sec N]

Restart the Colima LaunchDaemon and wait for Docker to become reachable.

Options:
  --profile NAME     Colima profile name (default: default)
  --user USER        User account that owns/runs Colima (default: current user)
  --home PATH        Home directory for the selected user (default: detected from dscl/$HOME)
  --label LABEL      LaunchDaemon label (default: com.nexus.colima.<user>.<profile>)
  --timeout-sec N    Wait time for Docker daemon health (default: 75)
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

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    sudo -H -u "${TARGET_USER}" HOME="${TARGET_HOME}" "${DOCKER_BIN}" "$@"
  else
    HOME="${TARGET_HOME}" "${DOCKER_BIN}" "$@"
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
    --label)
      LABEL="${2:-}"
      shift 2
      ;;
    --timeout-sec)
      TIMEOUT_SEC="${2:-}"
      shift 2
      ;;
    -h|--help)
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

ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd dscl "dscl" || exit 1
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  ns_require_cmd sudo "sudo" || exit 1
fi

DOCKER_BIN="$(command -v docker || true)"
TARGET_USER="$(resolve_target_user)"
[[ "${TARGET_USER}" != "root" ]] || ns_die "Colima target user must not be root"
TARGET_HOME="$(resolve_home_for_user "$TARGET_USER")"
SANITIZED_PROFILE="$(sanitize_profile "$PROFILE")"
SANITIZED_USER="$(sanitize_profile "$TARGET_USER")"
if [[ -z "${LABEL:-}" ]]; then
  LABEL="com.nexus.colima.${SANITIZED_USER}.${SANITIZED_PROFILE}"
fi

PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"

ns_print_header "Restarting Colima LaunchDaemon"

if sudo launchctl print "system/${LABEL}" >/dev/null 2>&1; then
  ns_print_ok "LaunchDaemon is loaded: ${LABEL}"
else
  if [[ ! -f "$PLIST_PATH" ]]; then
    ns_print_error "Colima LaunchDaemon not found: ${PLIST_PATH}"
    ns_print_warn "Install it first:"
    ns_print_warn "  ./deploy/scripts/install-colima-launchd.sh --profile ${PROFILE} --user ${TARGET_USER}"
    exit 1
  fi
  ns_print_warn "LaunchDaemon is not loaded; bootstrapping ${PLIST_PATH}"
  sudo launchctl bootstrap system "$PLIST_PATH"
fi

sudo launchctl kickstart -k "system/${LABEL}"

ns_print_header "Waiting for Docker via Colima"
run_docker_for_target context use colima >/dev/null 2>&1 || true
if wait_for_docker_as_target "$TIMEOUT_SEC"; then
  ns_print_ok "Docker daemon is reachable via Colima"
  exit 0
fi

ns_print_error "Docker daemon did not become reachable within ${TIMEOUT_SEC}s"
sudo launchctl print "system/${LABEL}" || true
exit 1
