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

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  ns_die "Restart the Colima launch agent as your normal macOS user, not as root"
fi

PROFILE="default"
LABEL=""
TIMEOUT_SEC="75"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restart-colima.sh [--profile NAME] [--label LABEL] [--timeout-sec N]

Restart the per-user Colima LaunchAgent and wait for Docker to become reachable.

Options:
  --profile NAME     Colima profile name (default: default)
  --label LABEL      LaunchAgent label (default: com.nexus.colima.<profile>)
  --timeout-sec N    Wait time for Docker daemon health (default: 75)
EOF
}

sanitize_profile() {
  printf '%s' "${1:-default}" | tr -c 'A-Za-z0-9._-' '_'
}

launchd_domain_for_user() {
  local uid
  uid="$(id -u)"
  if launchctl print "gui/${uid}" >/dev/null 2>&1; then
    echo "gui/${uid}"
  else
    echo "user/${uid}"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:-}"
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

SANITIZED_PROFILE="$(sanitize_profile "$PROFILE")"
if [[ -z "${LABEL:-}" ]]; then
  LABEL="com.nexus.colima.${SANITIZED_PROFILE}"
fi

PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LAUNCHD_DOMAIN="$(launchd_domain_for_user)"

ns_print_header "Restarting Colima LaunchAgent"

if launchctl print "${LAUNCHD_DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  ns_print_ok "LaunchAgent is loaded: ${LABEL}"
else
  if [[ ! -f "$PLIST_PATH" ]]; then
    ns_print_error "Colima LaunchAgent not found: ${PLIST_PATH}"
    ns_print_warn "Install it first:"
    ns_print_warn "  ./deploy/scripts/install-colima-launchd.sh --profile ${PROFILE}"
    exit 1
  fi
  ns_print_warn "LaunchAgent is not loaded; bootstrapping ${PLIST_PATH}"
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_PATH"
fi

launchctl kickstart -k "${LAUNCHD_DOMAIN}/${LABEL}"

ns_print_header "Waiting for Docker via Colima"
docker context use colima >/dev/null 2>&1 || true
if ns_wait_for_docker_daemon "$TIMEOUT_SEC"; then
  ns_print_ok "Docker daemon is reachable via Colima"
  exit 0
fi

ns_print_error "Docker daemon did not become reachable within ${TIMEOUT_SEC}s"
launchctl print "${LAUNCHD_DOMAIN}/${LABEL}" || true
exit 1
