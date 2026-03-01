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

python_is_supported_for_mlx() {
  local py_bin="${1:-}"
  local ver
  local major
  local minor

  if [[ -z "$py_bin" ]]; then
    return 1
  fi

  ver="$($py_bin -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
  if [[ -z "$ver" ]]; then
    return 1
  fi

  major="${ver%%.*}"
  minor="${ver##*.}"

  if [[ "$major" -gt 3 ]]; then
    return 0
  fi
  if [[ "$major" -eq 3 && "$minor" -ge 11 ]]; then
    return 0
  fi
  return 1
}

choose_python_for_mlx() {
  local candidates=()
  local candidate
  local resolved

  if [[ -n "${MLX_PYTHON:-}" ]]; then
    candidates+=("${MLX_PYTHON}")
  fi
  candidates+=(python3.12 python3.11 python3)

  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      resolved="$candidate"
    else
      resolved="$(command -v "$candidate" 2>/dev/null || true)"
    fi
    if [[ -n "$resolved" ]] && python_is_supported_for_mlx "$resolved"; then
      echo "$resolved"
      return 0
    fi
  done

  return 1
}

MLX_USER="${MLX_USER:-mlx}"
MLX_HOST="${MLX_HOST:-127.0.0.1}"
MLX_PORT="${MLX_PORT:-10240}"
MLX_MODEL_PATH="${MLX_MODEL_PATH:-mlx-community/gemma-2-2b-it-8bit}"
MLX_MODEL_TYPE="${MLX_MODEL_TYPE:-lm}"
MLX_HOME="${MLX_HOME:-/var/lib/mlx}"
MLX_VENV="${MLX_VENV:-/var/lib/mlx/env}"
MLX_LOG_DIR="${MLX_LOG_DIR:-/var/log/mlx}"
MLX_PIP_PACKAGES="${MLX_PIP_PACKAGES:-mlx-openai-server}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.nexus.mlx.openai.server}"
PLIST_PATH="/Library/LaunchDaemons/${LAUNCHD_LABEL}.plist"
CREATE_USER="${CREATE_USER:-1}"

usage() {
  cat <<'EOF'
Usage: services/mlx/scripts/install-native-macos.sh [--model-path PATH] [--model-type TYPE] [--port PORT]

Installs/starts MLX OpenAI server as a launchd service under an unprivileged user.

Options:
  --model-path PATH   Model path/repo id (default: mlx-community/gemma-2-2b-it-8bit)
  --model-type TYPE   Model type (default: lm)
  --host HOST         Listen host (default: 127.0.0.1)
  --port PORT         Listen port (default: 10240)
  --user USER         Service user (default: mlx)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model-path)
      MLX_MODEL_PATH="${2:-}"
      shift 2
      ;;
    --model-type)
      MLX_MODEL_TYPE="${2:-}"
      shift 2
      ;;
    --host)
      MLX_HOST="${2:-}"
      shift 2
      ;;
    --port)
      MLX_PORT="${2:-}"
      shift 2
      ;;
    --user)
      MLX_USER="${2:-}"
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

if [[ ! "$MLX_PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: invalid --port value: ${MLX_PORT}" >&2
  exit 2
fi

arch="$(uname -m 2>/dev/null || true)"
if [[ "$arch" != "arm64" ]]; then
  echo "WARNING: MLX acceleration requires Apple Silicon (arm64). Detected: ${arch}" >&2
fi

require_cmd sudo
require_cmd launchctl
require_cmd plutil
require_cmd dscl

MLX_PYTHON_BIN="$(choose_python_for_mlx || true)"
if [[ -z "$MLX_PYTHON_BIN" ]]; then
  echo "ERROR: could not find Python >=3.11 required by mlx-openai-server." >&2
  echo "Install one (example: brew install python@3.12), then re-run with:" >&2
  echo "  MLX_PYTHON=/opt/homebrew/bin/python3.12 ./services/mlx/scripts/install-native-macos.sh --host ${MLX_HOST} --port ${MLX_PORT}" >&2
  exit 1
fi

if ! id -u "${MLX_USER}" >/dev/null 2>&1; then
  if [[ "$CREATE_USER" != "1" ]]; then
    echo "ERROR: user '${MLX_USER}' does not exist (set CREATE_USER=1 to auto-create)." >&2
    exit 1
  fi

  echo "Creating service user '${MLX_USER}'..." >&2
  next_uid="$(dscl . -list /Users UniqueID 2>/dev/null | awk '{print $2}' | sort -n | tail -1)"
  next_uid="${next_uid:-500}"
  next_uid="$((next_uid + 1))"

  sudo dscl . -create "/Users/${MLX_USER}"
  sudo dscl . -create "/Users/${MLX_USER}" UserShell /usr/bin/false
  sudo dscl . -create "/Users/${MLX_USER}" RealName "Nexus MLX Service"
  sudo dscl . -create "/Users/${MLX_USER}" UniqueID "${next_uid}"
  sudo dscl . -create "/Users/${MLX_USER}" PrimaryGroupID 20
  sudo dscl . -create "/Users/${MLX_USER}" NFSHomeDirectory "${MLX_HOME}"
fi

sudo mkdir -p "${MLX_HOME}/cache" "${MLX_HOME}/run" "${MLX_LOG_DIR}"
sudo chown -R "${MLX_USER}:staff" "${MLX_HOME}" "${MLX_LOG_DIR}"
sudo chmod 750 "${MLX_HOME}" "${MLX_HOME}/cache" "${MLX_HOME}/run" "${MLX_LOG_DIR}"

if [[ -x "${MLX_VENV}/bin/python" ]]; then
  if ! python_is_supported_for_mlx "${MLX_VENV}/bin/python"; then
    echo "Existing MLX venv uses Python <3.11; recreating ${MLX_VENV}" >&2
    sudo rm -rf "${MLX_VENV}"
  fi
fi

sudo mkdir -p "${MLX_VENV}"
sudo chown -R root:wheel "${MLX_VENV}"
sudo chmod -R go-w "${MLX_VENV}"

if [[ ! -x "${MLX_VENV}/bin/python" ]]; then
  sudo -H "$MLX_PYTHON_BIN" -m venv "${MLX_VENV}"
fi

sudo -H "${MLX_VENV}/bin/python" -m pip install --upgrade --no-cache-dir pip setuptools wheel
# shellcheck disable=SC2086
sudo -H "${MLX_VENV}/bin/python" -m pip install --upgrade --no-cache-dir ${MLX_PIP_PACKAGES}

if [[ ! -x "${MLX_VENV}/bin/mlx-openai-server" ]]; then
  echo "ERROR: mlx-openai-server executable was not installed into ${MLX_VENV}/bin" >&2
  exit 1
fi

sudo chown root:wheel "${MLX_VENV}/bin/mlx-openai-server" 2>/dev/null || true
sudo chmod 755 "${MLX_VENV}/bin/mlx-openai-server" 2>/dev/null || true

sudo tee "${PLIST_PATH}" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LAUNCHD_LABEL}</string>

    <key>UserName</key>
    <string>${MLX_USER}</string>
    <key>WorkingDirectory</key>
    <string>${MLX_HOME}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${MLX_VENV}/bin/mlx-openai-server</string>
      <string>launch</string>
      <string>--model-path</string>
      <string>${MLX_MODEL_PATH}</string>
      <string>--model-type</string>
      <string>${MLX_MODEL_TYPE}</string>
      <string>--host</string>
      <string>${MLX_HOST}</string>
      <string>--port</string>
      <string>${MLX_PORT}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>EnvironmentVariables</key>
    <dict>
      <key>HOME</key>
      <string>${MLX_HOME}</string>
      <key>HF_HOME</key>
      <string>${MLX_HOME}/cache/huggingface</string>
      <key>XDG_CACHE_HOME</key>
      <string>${MLX_HOME}/cache</string>
    </dict>

    <key>StandardOutPath</key>
    <string>${MLX_LOG_DIR}/mlx-openai.out.log</string>
    <key>StandardErrorPath</key>
    <string>${MLX_LOG_DIR}/mlx-openai.err.log</string>
  </dict>
</plist>
EOF

sudo chown root:wheel "${PLIST_PATH}"
sudo chmod 644 "${PLIST_PATH}"
sudo plutil -lint "${PLIST_PATH}" >/dev/null

sudo launchctl bootout "system/${LAUNCHD_LABEL}" 2>/dev/null || true
sudo launchctl bootstrap system "${PLIST_PATH}"
sudo launchctl kickstart -k "system/${LAUNCHD_LABEL}"

echo "Installed ${LAUNCHD_LABEL} (${MLX_USER}) on ${MLX_HOST}:${MLX_PORT}" >&2
echo "Health check: curl -fsS http://${MLX_HOST}:${MLX_PORT}/v1/models" >&2
