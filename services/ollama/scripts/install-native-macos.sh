#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s 2>/dev/null || echo unknown)" != "Darwin" ]]; then
  echo "ERROR: this installer is macOS-only." >&2
  exit 1
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

OLLAMA_USER="${OLLAMA_USER:-ollama}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-/var/lib/ollama/models}"
OLLAMA_HOME="${OLLAMA_HOME:-/var/lib/ollama}"
OLLAMA_LOG_DIR="${OLLAMA_LOG_DIR:-/var/log/ollama}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.nexus.ollama.server}"
PLIST_PATH="/Library/LaunchDaemons/${LAUNCHD_LABEL}.plist"
CREATE_USER="${CREATE_USER:-1}"

usage() {
  cat <<'EOF'
Usage: services/ollama/scripts/install-native-macos.sh [--host HOST] [--port PORT] [--user USER]

Installs/starts Ollama as a launchd service under an unprivileged user.

Options:
  --host HOST   Listen host (default: 127.0.0.1)
  --port PORT   Listen port (default: 11434)
  --user USER   Service user (default: ollama)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      OLLAMA_HOST="${2:-}"
      shift 2
      ;;
    --port)
      OLLAMA_PORT="${2:-}"
      shift 2
      ;;
    --user)
      OLLAMA_USER="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! "$OLLAMA_PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: invalid --port value: ${OLLAMA_PORT}" >&2
  exit 2
fi

require_cmd sudo
require_cmd launchctl
require_cmd plutil
require_cmd dscl

if ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing Ollama via Homebrew..." >&2
    brew install ollama
  else
    echo "ERROR: 'ollama' is not installed and Homebrew was not found." >&2
    echo "Install Ollama first: https://ollama.com/download" >&2
    exit 1
  fi
fi

OLLAMA_BIN="$(command -v ollama)"

if ! id -u "${OLLAMA_USER}" >/dev/null 2>&1; then
  if [[ "$CREATE_USER" != "1" ]]; then
    echo "ERROR: user '${OLLAMA_USER}' does not exist (set CREATE_USER=1 to auto-create)." >&2
    exit 1
  fi

  echo "Creating service user '${OLLAMA_USER}'..." >&2
  next_uid="$(dscl . -list /Users UniqueID 2>/dev/null | awk '{print $2}' | sort -n | tail -1)"
  next_uid="${next_uid:-500}"
  next_uid="$((next_uid + 1))"

  sudo dscl . -create "/Users/${OLLAMA_USER}"
  sudo dscl . -create "/Users/${OLLAMA_USER}" UserShell /usr/bin/false
  sudo dscl . -create "/Users/${OLLAMA_USER}" RealName "Nexus Ollama Service"
  sudo dscl . -create "/Users/${OLLAMA_USER}" UniqueID "${next_uid}"
  sudo dscl . -create "/Users/${OLLAMA_USER}" PrimaryGroupID 20
  sudo dscl . -create "/Users/${OLLAMA_USER}" NFSHomeDirectory "${OLLAMA_HOME}"
fi

sudo mkdir -p "${OLLAMA_HOME}" "${OLLAMA_MODELS_DIR}" "${OLLAMA_LOG_DIR}"
sudo chown -R "${OLLAMA_USER}:staff" "${OLLAMA_HOME}" "${OLLAMA_LOG_DIR}"
sudo chmod 750 "${OLLAMA_HOME}" "${OLLAMA_LOG_DIR}"
sudo chmod 750 "${OLLAMA_MODELS_DIR}"

sudo tee "${PLIST_PATH}" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LAUNCHD_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${OLLAMA_BIN}</string>
      <string>serve</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>UserName</key>
    <string>${OLLAMA_USER}</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>HOME</key>
      <string>${OLLAMA_HOME}</string>
      <key>OLLAMA_HOST</key>
      <string>${OLLAMA_HOST}:${OLLAMA_PORT}</string>
      <key>OLLAMA_MODELS</key>
      <string>${OLLAMA_MODELS_DIR}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>${OLLAMA_LOG_DIR}/ollama.out.log</string>
    <key>StandardErrorPath</key>
    <string>${OLLAMA_LOG_DIR}/ollama.err.log</string>
  </dict>
</plist>
EOF

sudo chown root:wheel "${PLIST_PATH}"
sudo chmod 644 "${PLIST_PATH}"
sudo plutil -lint "${PLIST_PATH}" >/dev/null

sudo launchctl bootout "system/${LAUNCHD_LABEL}" 2>/dev/null || true
sudo launchctl bootstrap system "${PLIST_PATH}"
sudo launchctl kickstart -k "system/${LAUNCHD_LABEL}"

echo "Installed ${LAUNCHD_LABEL} (${OLLAMA_USER}) on ${OLLAMA_HOST}:${OLLAMA_PORT}" >&2
echo "Health check: curl -fsS http://${OLLAMA_HOST}:${OLLAMA_PORT}/api/version" >&2
