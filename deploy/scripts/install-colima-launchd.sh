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

PROFILE="default"
VM_TYPE=""
START_INTERVAL="60"
WAIT_TIMEOUT="75"
NO_WAIT="false"
SANITIZED_PROFILE="default"
TARGET_USER=""
TARGET_HOME=""
TARGET_COLIMA_HOME="${COLIMA_HOME:-}"
TARGET_COLIMA_USER_HOME="${COLIMA_USER_HOME:-}"
SANITIZED_USER=""
LABEL=""
COLIMA_RUNTIME_ROOT="${COLIMA_RUNTIME_ROOT:-/var/lib/nexus-colima}"
COLIMA_LOG_DIR="${COLIMA_LOG_DIR:-/var/log/nexus-colima}"
declare -a EXTRA_MOUNTS=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/install-colima-launchd.sh [--profile NAME] [--vm-type TYPE] [--start-interval SEC] [--wait-timeout SEC] [--no-wait] [--user USER] [--home PATH] [--colima-home PATH] [--colima-user-home PATH] [--runtime-root PATH] [--log-dir PATH] [--label LABEL] [--mount PATH[:MODE]]

Install/reload a macOS LaunchDaemon that starts Colima at boot and runs it
under the selected unprivileged user account.

Options:
  --profile NAME        Colima profile name (default: default)
  --vm-type TYPE        Optional Colima vm-type override (for example: qemu)
  --start-interval SEC  Relaunch check interval in seconds (default: 60)
  --wait-timeout SEC    Seconds to wait for Docker readiness after install (default: 75)
  --no-wait             Skip Docker readiness wait and return after launchd reload
  --user USER           User account that should own/run Colima (default: current user)
  --home PATH           Home directory for the selected user (default: detected from dscl/$HOME)
  --colima-home PATH    Colima state root to export as COLIMA_HOME (default: existing COLIMA_HOME)
  --colima-user-home PATH
                        Home/config root to export as COLIMA_USER_HOME and HOME for docker/colima commands
  --runtime-root PATH   Root-owned asset directory for the Colima launch helper (default: /var/lib/nexus-colima)
  --log-dir PATH        Log directory for the Colima launch helper (default: /var/log/nexus-colima)
  --label LABEL         LaunchDaemon label (default: com.nexus.colima.<user>.<profile>)
  --mount PATH[:MODE]   Persist an explicit Colima host mount (repeatable). MODE defaults to w.
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
    sudo -H -u "${TARGET_USER}" HOME="${docker_home}" COLIMA_HOME="${TARGET_COLIMA_HOME:-}" "${DOCKER_BIN}" "$@"
  else
    HOME="${docker_home}" COLIMA_HOME="${TARGET_COLIMA_HOME:-}" "${DOCKER_BIN}" "$@"
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

append_mount_spec() {
  local candidate="$1"
  [[ -n "$candidate" ]] || return 0
  local existing
  for existing in "${EXTRA_MOUNTS[@]:-}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return 0
    fi
  done
  EXTRA_MOUNTS+=("$candidate")
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
    --wait-timeout)
      WAIT_TIMEOUT="${2:-}"
      shift 2
      ;;
    --no-wait)
      NO_WAIT="true"
      shift
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
    --log-dir)
      COLIMA_LOG_DIR="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
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
[[ "$START_INTERVAL" =~ ^[0-9]+$ ]] || ns_die "--start-interval must be an integer"
[[ "$WAIT_TIMEOUT" =~ ^[0-9]+$ ]] || ns_die "--wait-timeout must be an integer"
[[ "${COLIMA_RUNTIME_ROOT}" == /* ]] || ns_die "--runtime-root must be an absolute path"
[[ "${COLIMA_LOG_DIR}" == /* ]] || ns_die "--log-dir must be an absolute path"

ns_require_cmd colima "colima" || exit 1
ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd plutil "plutil" || exit 1
ns_require_cmd dscl "dscl" || exit 1
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  ns_require_cmd sudo "sudo" || exit 1
fi

COLIMA_BIN="$(command -v colima)"
DOCKER_BIN="$(command -v docker || true)"
TARGET_USER="$(resolve_target_user)"
[[ "${TARGET_USER}" != "root" ]] || ns_die "Colima target user must not be root"
TARGET_HOME="$(resolve_home_for_user "$TARGET_USER")"
TARGET_UID="$(id -u "$TARGET_USER")"
if [[ -z "${TARGET_COLIMA_USER_HOME:-}" ]]; then
  TARGET_COLIMA_USER_HOME="$TARGET_HOME"
fi

resolved_runtime_root="$(ns_runtime_root "$ROOT_DIR")"
append_mount_spec "$ROOT_DIR:w"
if [[ -n "${resolved_runtime_root:-}" && -e "$resolved_runtime_root" ]]; then
  append_mount_spec "$resolved_runtime_root:w"
fi

normalized_mounts=()
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

colima_mounts_csv=""
if [[ ${#normalized_mounts[@]} -gt 0 ]]; then
  printf -v colima_mounts_csv '%s,' "${normalized_mounts[@]}"
  colima_mounts_csv="${colima_mounts_csv%,}"
fi

SANITIZED_PROFILE="$(sanitize_profile "$PROFILE")"
SANITIZED_USER="$(sanitize_profile "$TARGET_USER")"
if [[ -z "${LABEL:-}" ]]; then
  LABEL="com.nexus.colima.${SANITIZED_USER}.${SANITIZED_PROFILE}"
fi

LAUNCHER_DST="${TARGET_HOME}/.colima/bin/nexus-colima-launch"
ENV_FILE="${TARGET_HOME}/.colima/${SANITIZED_USER}-${SANITIZED_PROFILE}.env"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
OUT_LOG="${COLIMA_LOG_DIR}/${LABEL}.out.log"
ERR_LOG="${COLIMA_LOG_DIR}/${LABEL}.err.log"

sudo install -d -o root -g wheel -m 755 "${COLIMA_RUNTIME_ROOT}"
sudo install -d -o "${TARGET_USER}" -g staff -m 700 "${TARGET_HOME}/.colima"
sudo install -d -o "${TARGET_USER}" -g staff -m 700 "${TARGET_HOME}/.colima/bin"
sudo install -d -o "${TARGET_USER}" -g staff -m 750 "${COLIMA_LOG_DIR}"

sudo install -o "${TARGET_USER}" -g staff -m 700 "${ROOT_DIR}/deploy/scripts/colima-launch-agent.sh" "$LAUNCHER_DST"

tmp_env_file="$(mktemp)"
cat >"$tmp_env_file" <<EOF
COLIMA_BIN=${COLIMA_BIN}
DOCKER_BIN=${DOCKER_BIN}
COLIMA_PROFILE=${PROFILE}
COLIMA_VM_TYPE=${VM_TYPE}
COLIMA_USER_HOME=${TARGET_COLIMA_USER_HOME}
COLIMA_HOME=${TARGET_COLIMA_HOME}
COLIMA_FALLBACK_HOME=${TARGET_COLIMA_HOME}
COLIMA_MOUNTS=${colima_mounts_csv}
EOF
sudo install -o root -g wheel -m 644 "$tmp_env_file" "$ENV_FILE"
rm -f "$tmp_env_file"

tmp_plist="$(mktemp)"
cat >"$tmp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>UserName</key>
    <string>${TARGET_USER}</string>
    <key>WorkingDirectory</key>
    <string>${TARGET_HOME}</string>

    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${LAUNCHER_DST}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>StartInterval</key>
    <integer>${START_INTERVAL}</integer>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
      <key>HOME</key>
      <string>${TARGET_HOME}</string>
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

sudo install -o root -g wheel -m 644 "$tmp_plist" "$PLIST_PATH"
rm -f "$tmp_plist"
sudo plutil -lint "$PLIST_PATH" >/dev/null

legacy_agent_plist="${TARGET_HOME}/Library/LaunchAgents/${LABEL}.plist"
if [[ -f "$legacy_agent_plist" ]]; then
  sudo launchctl bootout "gui/${TARGET_UID}/${LABEL}" >/dev/null 2>&1 || true
  sudo launchctl bootout "user/${TARGET_UID}/${LABEL}" >/dev/null 2>&1 || true
  sudo rm -f "$legacy_agent_plist"
  ns_print_warn "Removed legacy LaunchAgent ${legacy_agent_plist} to avoid duplicate Colima starters."
fi

sudo launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
sudo launchctl remove "${LABEL}" >/dev/null 2>&1 || true

bootstrap_err="$(mktemp)"
if ! sudo launchctl bootstrap system "$PLIST_PATH" 2>"$bootstrap_err"; then
  ns_print_error "launchctl bootstrap failed for ${LABEL}"
  if [[ -s "$bootstrap_err" ]]; then
    cat "$bootstrap_err" >&2
  fi
  rm -f "$bootstrap_err"
  ns_print_warn "Diagnostics:"
  ns_print_warn "  sudo plutil -lint '${PLIST_PATH}'"
  ns_print_warn "  sudo launchctl print system/${LABEL}"
  ns_print_warn "  sudo tail -n 120 '${ERR_LOG}'"
  ns_print_warn "  sudo tail -n 120 '${OUT_LOG}'"
  exit 1
fi
rm -f "$bootstrap_err"

sudo launchctl kickstart -k "system/${LABEL}"

ns_print_header "Waiting for Colima / Docker"
run_docker_for_target context use colima >/dev/null 2>&1 || true
if [[ "$NO_WAIT" == "true" ]]; then
  ns_print_warn "Skipped Docker readiness wait (--no-wait requested)"
else
  if wait_for_docker_as_target "$WAIT_TIMEOUT"; then
    ns_print_ok "Colima launch daemon is active (${LABEL})"
    ns_print_ok "Docker daemon is reachable via the Colima context"
  else
    ns_print_warn "LaunchDaemon was installed, but Docker is not reachable yet"
    ns_print_warn "Inspect logs:"
    ns_print_warn "  sudo tail -n 120 '${OUT_LOG}'"
    ns_print_warn "  sudo tail -n 120 '${ERR_LOG}'"
  fi
fi

echo "LaunchDaemon: ${PLIST_PATH}"
echo "Restart helper: ./deploy/scripts/restart-colima.sh --profile ${PROFILE}"
if [[ -n "${colima_mounts_csv:-}" ]]; then
  echo "Persisted mounts: ${colima_mounts_csv}"
fi
