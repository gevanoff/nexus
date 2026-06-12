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

TARGET_USER=""
TARGET_HOME=""
REPO_DIR="$ROOT_DIR"
NEXUS_ENV_FILE="$ROOT_DIR/.env"
BACKUP_DIR=""
CONTAINER_NAME=""
ENDPOINTS=""
KEEP_COUNT="30"
START_INTERVAL="21600"
SSH_TARGET=""
SSH_DIR=""
RCLONE_REMOTE=""
LABEL="${NEXUS_ETCD_BACKUP_LAUNCHD_LABEL:-com.nexus.etcd-backup}"
LAUNCHD_ROOT="${NEXUS_ETCD_BACKUP_LAUNCHD_ROOT:-/ai-data/launchd/nexus-etcd-backup}"
LOG_DIR=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/install-etcd-backup-launchd.sh [options]

Install or reload a macOS launchd job that runs recurring etcd snapshot backups.

Options:
  --user USER             User account that should run the backup job
  --home PATH             Home directory for that user
  --repo-dir PATH         Nexus repo path (default: current repo)
  --env-file PATH         Nexus .env file used to resolve the runtime root
  --backup-dir PATH       Directory for local snapshot backups
  --container NAME        etcd container name override
  --endpoints URLS        etcdctl endpoints override
  --keep COUNT            Number of local backups to retain (default: 30)
  --start-interval SEC    Backup interval in seconds (default: 21600)
  --ssh-target USER@HOST  Mirror the backup bundle to a remote host over SSH
  --ssh-dir PATH          Destination directory for --ssh-target
  --rclone-remote DEST    Mirror the backup bundle to a configured rclone destination
  --log-dir PATH          launchd stdout/stderr directory
  --label LABEL           launchd label (default: com.nexus.etcd-backup)
  --launchd-root PATH     Root-owned launchd asset root (default: /ai-data/launchd/nexus-etcd-backup)
EOF
}

resolve_runtime_root() {
  local repo_dir="$1"
  local env_file="$2"
  local configured_root=""

  configured_root="$(ns_env_get "$env_file" NEXUS_RUNTIME_ROOT "")"
  if [[ -z "$configured_root" ]]; then
    printf '%s\n' "${repo_dir}/.runtime"
    return 0
  fi
  case "$configured_root" in
    /*)
      printf '%s\n' "$configured_root"
      ;;
    *)
      printf '%s\n' "${repo_dir}/${configured_root#./}"
      ;;
  esac
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
    ns_die "When running as root, pass --user USER"
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
  if [[ "$user_name" == "$(id -un)" && -n "${HOME:-}" ]]; then
    printf '%s\n' "$HOME"
    return 0
  fi
  home_dir="$(dscl . -read "/Users/${user_name}" NFSHomeDirectory 2>/dev/null | awk '{print $2}' | tail -n 1 || true)"
  [[ -n "${home_dir:-}" ]] || ns_die "Could not determine home directory for user '${user_name}'"
  printf '%s\n' "$home_dir"
}

launchctl_for_target() {
  if [[ "$TARGET_USER" == "$(id -un)" ]]; then
    launchctl "$@"
  else
    sudo -H -u "$TARGET_USER" launchctl "$@"
  fi
}

canonical_path() {
  local path="$1"
  python3 - "$path" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve(strict=False))
PY
}

append_env_line() {
  local key="$1"
  local value="$2"
  printf '%s=%q\n' "$key" "$value" >>"$JOB_ENV_FILE_TMP"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      TARGET_USER="${2:-}"
      shift 2
      ;;
    --home)
      TARGET_HOME="${2:-}"
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="${2:-}"
      shift 2
      ;;
    --env-file)
      NEXUS_ENV_FILE="${2:-}"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="${2:-}"
      shift 2
      ;;
    --container)
      CONTAINER_NAME="${2:-}"
      shift 2
      ;;
    --endpoints)
      ENDPOINTS="${2:-}"
      shift 2
      ;;
    --keep)
      KEEP_COUNT="${2:-}"
      shift 2
      ;;
    --start-interval)
      START_INTERVAL="${2:-}"
      shift 2
      ;;
    --ssh-target)
      SSH_TARGET="${2:-}"
      shift 2
      ;;
    --ssh-dir)
      SSH_DIR="${2:-}"
      shift 2
      ;;
    --rclone-remote)
      RCLONE_REMOTE="${2:-}"
      shift 2
      ;;
    --log-dir)
      LOG_DIR="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
      shift 2
      ;;
    --launchd-root)
      LAUNCHD_ROOT="${2:-}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) ns_die "Unknown argument: $1" ;;
  esac
done

[[ "$START_INTERVAL" =~ ^[0-9]+$ ]] || ns_die "--start-interval must be a non-negative integer"
[[ "$KEEP_COUNT" =~ ^[0-9]+$ ]] || ns_die "--keep must be a non-negative integer"
[[ "$LAUNCHD_ROOT" == /* ]] || ns_die "--launchd-root must be an absolute path"

ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd plutil "plutil" || exit 1
ns_require_cmd dscl "dscl" || exit 1
ns_require_cmd python3 "python3" || exit 1
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  ns_require_cmd sudo "sudo" || exit 1
fi

TARGET_USER="$(resolve_target_user)"
TARGET_HOME="$(resolve_home_for_user "$TARGET_USER")"
RUNTIME_ROOT="$(resolve_runtime_root "$REPO_DIR" "$NEXUS_ENV_FILE")"

if [[ -z "$BACKUP_DIR" ]]; then
  BACKUP_DIR="${RUNTIME_ROOT}/etcd/backups"
fi
if [[ -z "$LOG_DIR" ]]; then
  LOG_DIR="${BACKUP_DIR}/logs"
fi

[[ "$BACKUP_DIR" == /* ]] || ns_die "--backup-dir must be an absolute path"
[[ "$LOG_DIR" == /* ]] || ns_die "--log-dir must be an absolute path"

BIN_DIR="${LAUNCHD_ROOT}/bin"
LAUNCHD_DIR="${LAUNCHD_ROOT}/plists"
ENV_DIR="${LAUNCHD_ROOT}/env"
LAUNCHER_DST="${BIN_DIR}/etcd-backup-launch-agent.sh"
JOB_ENV_FILE="${ENV_DIR}/etcd-backup.env"
PLIST_PATH="${LAUNCHD_DIR}/${LABEL}.plist"
OUT_LOG="${LOG_DIR}/${LABEL}.out.log"
ERR_LOG="${LOG_DIR}/${LABEL}.err.log"

sudo install -d -o root -g wheel -m 755 "$LAUNCHD_ROOT" "$BIN_DIR" "$LAUNCHD_DIR" "$ENV_DIR"
sudo install -d -o "$TARGET_USER" -g staff -m 750 "$BACKUP_DIR" "$LOG_DIR"
sudo install -o root -g wheel -m 755 "$ROOT_DIR/deploy/scripts/etcd-backup-launch-agent.sh" "$LAUNCHER_DST"

JOB_ENV_FILE_TMP="$(mktemp)"
append_env_line "NEXUS_REPO_DIR" "$REPO_DIR"
append_env_line "ENV_FILE" "$NEXUS_ENV_FILE"
append_env_line "BACKUP_DIR" "$BACKUP_DIR"
append_env_line "KEEP_COUNT" "$KEEP_COUNT"
if [[ -n "$CONTAINER_NAME" ]]; then
  append_env_line "CONTAINER_NAME" "$CONTAINER_NAME"
fi
if [[ -n "$ENDPOINTS" ]]; then
  append_env_line "ENDPOINTS" "$ENDPOINTS"
fi
if [[ -n "$SSH_TARGET" ]]; then
  append_env_line "SSH_TARGET" "$SSH_TARGET"
fi
if [[ -n "$SSH_DIR" ]]; then
  append_env_line "SSH_DIR" "$SSH_DIR"
fi
if [[ -n "$RCLONE_REMOTE" ]]; then
  append_env_line "RCLONE_REMOTE" "$RCLONE_REMOTE"
fi
sudo install -o "$TARGET_USER" -g staff -m 600 "$JOB_ENV_FILE_TMP" "$JOB_ENV_FILE"
rm -f "$JOB_ENV_FILE_TMP"

PLIST_LAUNCHER_DST="$(canonical_path "$LAUNCHER_DST")"
PLIST_JOB_ENV_FILE="$(canonical_path "$JOB_ENV_FILE")"

tmp_plist="$(mktemp)"
cat >"$tmp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>WorkingDirectory</key>
    <string>/</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>-lc</string>
      <string>exec /usr/bin/sudo -H -u ${TARGET_USER} env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin HOME=${TARGET_HOME} NEXUS_ETCD_BACKUP_ENV_FILE=${PLIST_JOB_ENV_FILE} /bin/bash ${PLIST_LAUNCHER_DST}</string>
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

TARGET_UID="$(id -u "$TARGET_USER")"
sudo launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
launchctl_for_target bootout "gui/${TARGET_UID}/${LABEL}" >/dev/null 2>&1 || true
sudo launchctl bootstrap system "$PLIST_PATH"
sudo launchctl enable "system/${LABEL}"
sudo launchctl kickstart -k "system/${LABEL}"

ns_print_ok "Installed launchd job ${LABEL}"
echo "Env file: ${JOB_ENV_FILE}"
echo "Plist: ${PLIST_PATH}"
echo "Logs: ${OUT_LOG} / ${ERR_LOG}"
