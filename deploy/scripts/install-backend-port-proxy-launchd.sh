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
RUNTIME_ROOT="${NEXUS_BACKEND_PROXY_RUNTIME_ROOT:-/ai-data/var/lib/nexus-backend-proxy}"
LAUNCHD_ROOT="${NEXUS_BACKEND_PROXY_LAUNCHD_ROOT:-/usr/local/nexus-backend-proxy}"
LOG_DIR="${NEXUS_BACKEND_PROXY_LOG_DIR:-/var/log/nexus-backend-proxy}"
LABEL="${NEXUS_BACKEND_PROXY_LAUNCHD_LABEL:-com.nexus.backend-port-proxy}"
PYTHON_BIN="${PYTHON_BIN:-}"
CONNECT_TIMEOUT="${NEXUS_BACKEND_PROXY_CONNECT_TIMEOUT_SEC:-10}"
USE_DEFAULT_FORWARDS="true"
FORWARDS=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/install-backend-port-proxy-launchd.sh [options]

Install/reload a macOS launchd job for host-side Nexus backend TCP proxies.
This is intended for ai2 when Gateway runs inside Colima and the Colima VM
cannot reach remote LAN model backends directly.

Options:
  --user USER             User account that should run the proxy
  --home PATH             Home directory for that user
  --runtime-root PATH     Runtime root (default: /ai-data/var/lib/nexus-backend-proxy)
  --launchd-root PATH     Root-owned launchd asset root (default: /usr/local/nexus-backend-proxy)
  --log-dir PATH          launchd stdout/stderr directory (default: /var/log/nexus-backend-proxy)
  --label LABEL           launchd label (default: com.nexus.backend-port-proxy)
  --python PATH           Python interpreter (default: command -v python3)
  --connect-timeout SEC   Upstream connect timeout (default: 10)
  --forward SPEC          Forward spec: NAME=LISTEN_HOST:LISTEN_PORT=TARGET_HOST:TARGET_PORT
  --no-default-forwards   Do not install the production default forwards

Default forwards:
  vllm-fast=127.0.0.1:18001=stackrot:8001
  vllm-embeddings=127.0.0.1:18002=meltdown:8002
  vllm=127.0.0.1:18003=ada2:8003
  images=127.0.0.1:17860=ada2:7860
  sdxl-turbo=127.0.0.1:18050=meltdown:9050
  lighton-ocr=127.0.0.1:18155=ada2:9155
  personaplex=127.0.0.1:18160=ada2:9160
  skyreels-v2=127.0.0.1:18180=ada2:9180
  ssh-stackrot=127.0.0.1:19022=stackrot:22
  ssh-ada2=127.0.0.1:19023=ada2:22
  ssh-meltdown=127.0.0.1:19024=meltdown:22
  ssh-copyfail=127.0.0.1:19025=copyfail:22
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

canonical_path() {
  local path="$1"
  python3 - "$path" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve(strict=False))
PY
}

xml_escape() {
  python3 - "$1" <<'PY'
import html
import sys

print(html.escape(sys.argv[1], quote=False))
PY
}

validate_forward() {
  local spec="$1"
  python3 "$ROOT_DIR/deploy/scripts/backend-port-proxy.py" --forward "$spec" --check
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) TARGET_USER="${2:-}"; shift 2 ;;
    --home) TARGET_HOME="${2:-}"; shift 2 ;;
    --runtime-root) RUNTIME_ROOT="${2:-}"; shift 2 ;;
    --launchd-root) LAUNCHD_ROOT="${2:-}"; shift 2 ;;
    --log-dir) LOG_DIR="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    --python) PYTHON_BIN="${2:-}"; shift 2 ;;
    --connect-timeout) CONNECT_TIMEOUT="${2:-}"; shift 2 ;;
    --forward) FORWARDS+=("${2:-}"); shift 2 ;;
    --no-default-forwards) USE_DEFAULT_FORWARDS="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) ns_die "Unknown argument: $1" ;;
  esac
done

[[ "$CONNECT_TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]] || ns_die "--connect-timeout must be numeric"
[[ "$RUNTIME_ROOT" == /ai-data/* ]] || ns_die "--runtime-root must be under /ai-data"
[[ "$LAUNCHD_ROOT" == /* ]] || ns_die "--launchd-root must be an absolute path"
[[ "$LOG_DIR" == /* ]] || ns_die "--log-dir must be an absolute path"

if [[ "$USE_DEFAULT_FORWARDS" == "true" ]]; then
  DEFAULT_FORWARDS=(
    "vllm-fast=127.0.0.1:18001=stackrot:8001"
    "vllm-embeddings=127.0.0.1:18002=meltdown:8002"
    "vllm=127.0.0.1:18003=ada2:8003"
    "images=127.0.0.1:17860=ada2:7860"
    "sdxl-turbo=127.0.0.1:18050=meltdown:9050"
    "lighton-ocr=127.0.0.1:18155=ada2:9155"
    "personaplex=127.0.0.1:18160=ada2:9160"
    "skyreels-v2=127.0.0.1:18180=ada2:9180"
    "ssh-stackrot=127.0.0.1:19022=stackrot:22"
    "ssh-ada2=127.0.0.1:19023=ada2:22"
    "ssh-meltdown=127.0.0.1:19024=meltdown:22"
    "ssh-copyfail=127.0.0.1:19025=copyfail:22"
  )
  if [[ ${#FORWARDS[@]} -gt 0 ]]; then
    FORWARDS=("${DEFAULT_FORWARDS[@]}" "${FORWARDS[@]}")
  else
    FORWARDS=("${DEFAULT_FORWARDS[@]}")
  fi
fi
[[ ${#FORWARDS[@]} -gt 0 ]] || ns_die "At least one --forward is required"

ns_require_cmd launchctl "launchctl" || exit 1
ns_require_cmd plutil "plutil" || exit 1
ns_require_cmd dscl "dscl" || exit 1
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  ns_require_cmd sudo "sudo" || exit 1
fi
if [[ -z "${PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
[[ -n "${PYTHON_BIN:-}" ]] || ns_die "python3 is required but not installed"

for spec in "${FORWARDS[@]}"; do
  validate_forward "$spec"
done

TARGET_USER="$(resolve_target_user)"
TARGET_HOME="$(resolve_home_for_user "$TARGET_USER")"
BIN_DIR="${LAUNCHD_ROOT}/bin"
PLIST_DIR="${LAUNCHD_ROOT}/plists"
PROXY_DST="${BIN_DIR}/backend-port-proxy.py"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
OUT_LOG="${LOG_DIR}/${LABEL}.out.log"
ERR_LOG="${LOG_DIR}/${LABEL}.err.log"

sudo install -d -o "${TARGET_USER}" -g staff -m 750 "$RUNTIME_ROOT" "$LOG_DIR"
sudo install -d -o root -g wheel -m 755 "$LAUNCHD_ROOT" "$BIN_DIR" "$PLIST_DIR"
sudo install -o root -g wheel -m 755 "$ROOT_DIR/deploy/scripts/backend-port-proxy.py" "$PROXY_DST"

PLIST_PYTHON_BIN="$(canonical_path "$PYTHON_BIN")"
PLIST_PROXY_DST="$(canonical_path "$PROXY_DST")"
PLIST_TARGET_HOME="$(xml_escape "$TARGET_HOME")"
PLIST_OUT_LOG="$(xml_escape "$(canonical_path "$OUT_LOG")")"
PLIST_ERR_LOG="$(xml_escape "$(canonical_path "$ERR_LOG")")"

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
      <string>${PLIST_PYTHON_BIN}</string>
      <string>${PLIST_PROXY_DST}</string>
      <string>--connect-timeout</string>
      <string>${CONNECT_TIMEOUT}</string>
EOF
for spec in "${FORWARDS[@]}"; do
  escaped_spec="$(xml_escape "$spec")"
  cat >>"$tmp_plist" <<EOF
      <string>--forward</string>
      <string>${escaped_spec}</string>
EOF
done
cat >>"$tmp_plist" <<EOF
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
      <key>HOME</key>
      <string>${PLIST_TARGET_HOME}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${PLIST_OUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${PLIST_ERR_LOG}</string>
  </dict>
</plist>
EOF
sudo install -o root -g wheel -m 644 "$tmp_plist" "$PLIST_PATH"
rm -f "$tmp_plist"
sudo plutil -lint "$PLIST_PATH" >/dev/null

# LaunchDaemons loaded from arbitrary paths are not rediscovered after a
# reboot. Keep the canonical plist in /Library/LaunchDaemons so the proxy is
# restored automatically with the rest of the ai2 control plane.
sudo rm -f "${PLIST_DIR}/${LABEL}.plist"

sudo launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
# launchd can briefly retain the old label after bootout and reject an
# immediate bootstrap from the canonical plist with error 5.
sleep 2
sudo launchctl bootstrap system "$PLIST_PATH"
sudo launchctl kickstart -k "system/${LABEL}" || true

ns_print_ok "Backend port proxy launchd job installed: system/${LABEL}"
ns_print_ok "Proxy script: ${PROXY_DST}"
ns_print_ok "Logs: ${LOG_DIR}"
