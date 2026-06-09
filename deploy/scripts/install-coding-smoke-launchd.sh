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
REPO_DIR="${ROOT_DIR}"
NEXUS_ENV_FILE="${ROOT_DIR}/.env"
RUNTIME_ROOT="${NEXUS_CODING_SMOKE_RUNTIME_ROOT:-/ai-data/var/lib/nexus-smoke}"
LAUNCHD_ROOT="${NEXUS_CODING_SMOKE_LAUNCHD_ROOT:-/ai-data/launchd/nexus-smoke}"
OUTPUT_DIR="${NEXUS_CODING_SMOKE_OUTPUT_DIR:-${RUNTIME_ROOT}/coding}"
LOG_DIR="${NEXUS_CODING_SMOKE_LOG_DIR:-${RUNTIME_ROOT}/logs}"
START_INTERVAL="${NEXUS_CODING_SMOKE_START_INTERVAL_SEC:-3600}"
MODELS="${NEXUS_CODING_SMOKE_MODELS:-coder}"
WEEKLY_MODELS="${NEXUS_CODING_SMOKE_WEEKLY_MODELS:-}"
LABEL="${NEXUS_CODING_SMOKE_LAUNCHD_LABEL:-com.nexus.coding-smoke}"
GATEWAY_URL="${NEXUS_GATEWAY_URL:-http://127.0.0.1:8800}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/install-coding-smoke-launchd.sh [options]

Install/reload a macOS launchd job for recurring Nexus Coding smoke tests.
All job-owned files are installed under /ai-data by default.

Options:
  --user USER             User account that should run the smoke test
  --home PATH             Home directory for that user
  --repo-dir PATH         Nexus repo path (default: current repo)
  --env-file PATH         Nexus .env file (default: repo .env)
  --runtime-root PATH     Smoke runtime root (default: /ai-data/var/lib/nexus-smoke)
  --launchd-root PATH     Root-owned launchd asset root (default: /ai-data/launchd/nexus-smoke)
  --output-dir PATH       JSON report directory (default: runtime-root/coding)
  --log-dir PATH          launchd stdout/stderr directory (default: runtime-root/logs)
  --start-interval SEC    Run interval in seconds (default: 3600)
  --models CSV            Regular model list (default: coder)
  --weekly-models CSV     Weekly idle-window model list for non-active huge models
  --gateway-url URL       Gateway URL (default: http://127.0.0.1:8800)
  --label LABEL           launchd label (default: com.nexus.coding-smoke)
EOF
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
  if [[ "${user_name}" == "$(id -un)" && -n "${HOME:-}" ]]; then
    printf '%s\n' "$HOME"
    return 0
  fi
  home_dir="$(dscl . -read "/Users/${user_name}" NFSHomeDirectory 2>/dev/null | awk '{print $2}' | tail -n 1 || true)"
  [[ -n "${home_dir:-}" ]] || ns_die "Could not determine home directory for user '${user_name}'"
  printf '%s\n' "$home_dir"
}

update_env_value() {
  local env_file="$1"
  local key="$2"
  local value="$3"
  python3 - "$env_file" "$key" "$value" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
pattern = re.compile(rf"^\s*{re.escape(key)}=")
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
replacement = f"{key}={value}"
for idx, raw in enumerate(lines):
    if pattern.match(raw):
        lines[idx] = replacement
        break
else:
    lines.append(replacement)
payload = "\n".join(lines)
if payload:
    payload += "\n"
path.write_text(payload, encoding="utf-8")
PY
  chmod 600 "$env_file" 2>/dev/null || true
}

canonical_path() {
  local path="$1"
  python3 - "$path" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve(strict=False))
PY
}

launchctl_for_target() {
  if [[ "$TARGET_USER" == "$(id -un)" ]]; then
    launchctl "$@"
  else
    sudo -H -u "$TARGET_USER" launchctl "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) TARGET_USER="${2:-}"; shift 2 ;;
    --home) TARGET_HOME="${2:-}"; shift 2 ;;
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --env-file) NEXUS_ENV_FILE="${2:-}"; shift 2 ;;
    --runtime-root) RUNTIME_ROOT="${2:-}"; shift 2 ;;
    --launchd-root) LAUNCHD_ROOT="${2:-}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --log-dir) LOG_DIR="${2:-}"; shift 2 ;;
    --start-interval) START_INTERVAL="${2:-}"; shift 2 ;;
    --models) MODELS="${2:-}"; shift 2 ;;
    --weekly-models) WEEKLY_MODELS="${2:-}"; shift 2 ;;
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) ns_die "Unknown argument: $1" ;;
  esac
done

[[ "$START_INTERVAL" =~ ^[0-9]+$ ]] || ns_die "--start-interval must be an integer"
[[ "$RUNTIME_ROOT" == /ai-data/* ]] || ns_die "--runtime-root must be under /ai-data"
[[ "$LAUNCHD_ROOT" == /ai-data/* ]] || ns_die "--launchd-root must be under /ai-data"
[[ "$OUTPUT_DIR" == /ai-data/* ]] || ns_die "--output-dir must be under /ai-data"
[[ "$LOG_DIR" == /ai-data/* ]] || ns_die "--log-dir must be under /ai-data"

ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd plutil "plutil" || exit 1
ns_require_cmd dscl "dscl" || exit 1
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  ns_require_cmd sudo "sudo" || exit 1
fi

TARGET_USER="$(resolve_target_user)"
TARGET_HOME="$(resolve_home_for_user "$TARGET_USER")"
BIN_DIR="${LAUNCHD_ROOT}/bin"
LAUNCHD_DIR="${LAUNCHD_ROOT}/plists"
ENV_DIR="${RUNTIME_ROOT}/coding"
LAUNCHER_DST="${BIN_DIR}/nexus-coding-smoke-launch"
JOB_ENV_FILE="${ENV_DIR}/coding-smoke.env"
PLIST_PATH="${LAUNCHD_DIR}/${LABEL}.plist"
OUT_LOG="${LOG_DIR}/${LABEL}.out.log"
ERR_LOG="${LOG_DIR}/${LABEL}.err.log"

sudo install -d -o "${TARGET_USER}" -g staff -m 750 "$BIN_DIR" "$ENV_DIR" "$OUTPUT_DIR" "$LOG_DIR"
sudo install -d -o root -g wheel -m 755 "$BIN_DIR" "$LAUNCHD_DIR"
sudo chown root:wheel "$BIN_DIR" "$LAUNCHD_DIR"
sudo chmod 755 "$BIN_DIR" "$LAUNCHD_DIR"
sudo install -o root -g wheel -m 755 "$ROOT_DIR/deploy/scripts/coding-smoke-launch-agent.sh" "$LAUNCHER_DST"

tmp_env="$(mktemp)"
cat >"$tmp_env" <<EOF
NEXUS_REPO_DIR=${REPO_DIR}
NEXUS_ENV_FILE=${NEXUS_ENV_FILE}
NEXUS_GATEWAY_URL=${GATEWAY_URL}
NEXUS_CODING_SMOKE_OUTPUT_DIR=${OUTPUT_DIR}
NEXUS_CODING_SMOKE_STDOUT_LOG=${OUT_LOG}
NEXUS_CODING_SMOKE_STDERR_LOG=${ERR_LOG}
NEXUS_CODING_SMOKE_MODELS=${MODELS}
NEXUS_CODING_SMOKE_WEEKLY_MODELS=${WEEKLY_MODELS}
EOF
sudo install -o "${TARGET_USER}" -g staff -m 600 "$tmp_env" "$JOB_ENV_FILE"
rm -f "$tmp_env"

update_env_value "$NEXUS_ENV_FILE" "NEXUS_CODING_SMOKE_OUTPUT_DIR" "$OUTPUT_DIR"

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
    <key>UserName</key>
    <string>${TARGET_USER}</string>
    <key>WorkingDirectory</key>
    <string>/</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${PLIST_LAUNCHER_DST}</string>
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
      <key>HOME</key>
      <string>${TARGET_HOME}</string>
      <key>NEXUS_CODING_SMOKE_ENV_FILE</key>
      <string>${PLIST_JOB_ENV_FILE}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>
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
sudo launchctl kickstart -k "system/${LABEL}" || true

ns_print_ok "Coding smoke launchd job installed: system/${LABEL}"
ns_print_ok "Reports: ${OUTPUT_DIR}"
ns_print_ok "Logs: ${LOG_DIR}"
ns_print_warn "The launchd plist lives under /ai-data. Re-run this installer after a full OS reinstall or launchd database reset."
