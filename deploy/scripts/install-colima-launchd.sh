#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

if [[ "$(ns_detect_platform)" != "macos" ]]; then
  ns_die "This installer is macOS-only"
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  ns_die "Install the Colima launch agent as your normal macOS user, not as root"
fi

PROFILE="default"
VM_TYPE=""
START_INTERVAL="60"
SANITIZED_PROFILE="default"
LABEL=""
APP_SUPPORT_ROOT="${APP_SUPPORT_ROOT:-${HOME}/Library/Application Support/Nexus}"
LOG_DIR="${HOME}/Library/Logs/Nexus"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/install-colima-launchd.sh [--profile NAME] [--vm-type TYPE] [--start-interval SEC] [--label LABEL]

Install/reload a per-user macOS LaunchAgent that ensures Colima starts after
login/reboot and periodically checks that the selected profile remains up.

Options:
  --profile NAME        Colima profile name (default: default)
  --vm-type TYPE        Optional Colima vm-type override (for example: qemu)
  --start-interval SEC  Relaunch check interval in seconds (default: 60)
  --label LABEL         LaunchAgent label (default: com.nexus.colima.<profile>)
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
    --vm-type)
      VM_TYPE="${2:-}"
      shift 2
      ;;
    --start-interval)
      START_INTERVAL="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
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
[[ "$START_INTERVAL" =~ ^[0-9]+$ ]] || ns_die "--start-interval must be an integer"

ns_require_cmd colima "colima" || exit 1
ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd plutil "plutil" || exit 1

COLIMA_BIN="$(command -v colima)"
DOCKER_BIN="$(command -v docker || true)"
SANITIZED_PROFILE="$(sanitize_profile "$PROFILE")"
if [[ -z "${LABEL:-}" ]]; then
  LABEL="com.nexus.colima.${SANITIZED_PROFILE}"
fi

LAUNCH_AGENT_DIR="${HOME}/Library/LaunchAgents"
COLIMA_RUNTIME_DIR="${APP_SUPPORT_ROOT}/colima"
LAUNCHER_DST="${COLIMA_RUNTIME_DIR}/bin/nexus-colima-launch"
ENV_FILE="${COLIMA_RUNTIME_DIR}/${SANITIZED_PROFILE}.env"
PLIST_PATH="${LAUNCH_AGENT_DIR}/${LABEL}.plist"
OUT_LOG="${LOG_DIR}/${LABEL}.out.log"
ERR_LOG="${LOG_DIR}/${LABEL}.err.log"
LAUNCHD_DOMAIN="$(launchd_domain_for_user)"

ns_mkdir_p "$LAUNCH_AGENT_DIR"
ns_mkdir_p "${COLIMA_RUNTIME_DIR}/bin"
ns_mkdir_p "$LOG_DIR"

cp "${ROOT_DIR}/deploy/scripts/colima-launch-agent.sh" "$LAUNCHER_DST"
chmod 755 "$LAUNCHER_DST"

cat >"$ENV_FILE" <<EOF
COLIMA_BIN=${COLIMA_BIN}
DOCKER_BIN=${DOCKER_BIN}
COLIMA_PROFILE=${PROFILE}
COLIMA_VM_TYPE=${VM_TYPE}
EOF
chmod 600 "$ENV_FILE"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${LAUNCHER_DST}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>StartInterval</key>
    <integer>${START_INTERVAL}</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
      <key>NEXUS_COLIMA_ENV_FILE</key>
      <string>${ENV_FILE}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>${OUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${ERR_LOG}</string>
  </dict>
</plist>
EOF

chmod 644 "$PLIST_PATH"
plutil -lint "$PLIST_PATH" >/dev/null

launchctl bootout "${LAUNCHD_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_PATH"
launchctl kickstart -k "${LAUNCHD_DOMAIN}/${LABEL}"

ns_print_header "Waiting for Colima / Docker"
docker context use colima >/dev/null 2>&1 || true
if ns_wait_for_docker_daemon 75; then
  ns_print_ok "Colima launch agent is active (${LABEL})"
  ns_print_ok "Docker daemon is reachable via the Colima context"
else
  ns_print_warn "LaunchAgent was installed, but Docker is not reachable yet"
  ns_print_warn "Inspect logs:"
  ns_print_warn "  tail -n 120 '${OUT_LOG}'"
  ns_print_warn "  tail -n 120 '${ERR_LOG}'"
fi

echo "LaunchAgent: ${PLIST_PATH}"
echo "Restart helper: ./deploy/scripts/restart-colima.sh --profile ${PROFILE}"
