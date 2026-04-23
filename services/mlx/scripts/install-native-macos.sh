#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s 2>/dev/null || echo unknown)" != "Darwin" ]]; then
  echo "ERROR: this installer is macOS-only." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_python.sh"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

choose_python_for_mlx() {
  ns_python_choose_at_least 3 11 "${MLX_PYTHON:-}" python3.12 python3.11 python3
}

lowercase_value() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

env_file_get() {
  local env_file="$1"
  local key="$2"
  local default_value="${3:-}"

  if [[ -z "${env_file:-}" || ! -f "$env_file" ]]; then
    echo "$default_value"
    return 0
  fi

  local line
  line="$(grep -E "^[[:space:]]*${key}=" "$env_file" 2>/dev/null | tail -n 1 || true)"
  if [[ -z "${line:-}" ]]; then
    echo "$default_value"
    return 0
  fi

  local value
  value="${line#*=}"
  value="${value%\r}"

  if [[ ${#value} -ge 2 ]]; then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi

  echo "$value"
}

has_shell_override() {
  [[ -n "${!1+x}" ]]
}

MLX_USER="${MLX_USER:-mlx}"
MLX_HOST="${MLX_HOST:-127.0.0.1}"
MLX_PORT="${MLX_PORT:-10240}"
MLX_MODEL_PATH="${MLX_MODEL_PATH:-mlx-community/gemma-3-4b-it-qat-4bit}"
MLX_MODEL_TYPE="${MLX_MODEL_TYPE:-lm}"
MLX_CONFIG_PATH="${MLX_CONFIG_PATH:-}"
MLX_HOME="${MLX_HOME:-/var/lib/mlx}"
MLX_VENV="${MLX_VENV:-/var/lib/mlx/env}"
MLX_LOG_DIR="${MLX_LOG_DIR:-/var/log/mlx}"
XDG_CACHE_HOME="${XDG_CACHE_HOME:-${MLX_HOME}/cache}"
HF_HOME="${HF_HOME:-${XDG_CACHE_HOME}/huggingface}"
MLX_PIP_PACKAGES="${MLX_PIP_PACKAGES:-mlx-openai-server}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.nexus.mlx.openai.server}"
PLIST_PATH="/Library/LaunchDaemons/${LAUNCHD_LABEL}.plist"
CREATE_USER="${CREATE_USER:-1}"
MLX_ENV_FILE="${MLX_ENV_FILE:-${MLX_HOME}/mlx.env}"
MLX_LAUNCHER="${MLX_VENV}/bin/mlx-openai-launch"
MLX_PREFETCHER="${MLX_VENV}/bin/mlx-prefetch-models"
MLX_PREFETCH_HELPER="${MLX_VENV}/bin/mlx-prefetch-models.py"
MLX_PREFETCH_HELPER_COMPAT="${MLX_VENV}/bin/prefetch_models.py"
PREFETCH_BEFORE_START="${PREFETCH_BEFORE_START:-1}"

HOST_FROM_SHELL="false"
PORT_FROM_SHELL="false"
MODEL_PATH_FROM_SHELL="false"
MODEL_TYPE_FROM_SHELL="false"
CONFIG_PATH_FROM_SHELL="false"
PREFETCH_FROM_CLI="false"
CACHE_HOME_FROM_SHELL="false"
HF_HOME_FROM_SHELL="false"

has_shell_override MLX_HOST && HOST_FROM_SHELL="true"
has_shell_override MLX_PORT && PORT_FROM_SHELL="true"
has_shell_override MLX_MODEL_PATH && MODEL_PATH_FROM_SHELL="true"
has_shell_override MLX_MODEL_TYPE && MODEL_TYPE_FROM_SHELL="true"
has_shell_override MLX_CONFIG_PATH && CONFIG_PATH_FROM_SHELL="true"
has_shell_override XDG_CACHE_HOME && CACHE_HOME_FROM_SHELL="true"
has_shell_override HF_HOME && HF_HOME_FROM_SHELL="true"

HOST_FROM_CLI="false"
PORT_FROM_CLI="false"
MODEL_PATH_FROM_CLI="false"
MODEL_TYPE_FROM_CLI="false"
CONFIG_PATH_FROM_CLI="false"
CACHE_HOME_FROM_CLI="false"
HF_HOME_FROM_CLI="false"

usage() {
  cat <<'EOF'
Usage: services/mlx/scripts/install-native-macos.sh [--model-path PATH] [--model-type TYPE] [--config PATH] [--port PORT]

Installs/starts MLX OpenAI server as a launchd service under an unprivileged user.

Options:
  --model-path PATH   Model path/repo id (default: mlx-community/gemma-3-4b-it-qat-4bit)
  --model-type TYPE   Model type (default: lm)
  --config PATH       Optional mlx-openai-server config YAML. When set, overrides --model-path/--model-type launch mode.
  --host HOST         Listen host (default: 127.0.0.1)
  --port PORT         Listen port (default: 10240)
  --cache-dir PATH    Cache root for MLX/Hugging Face artifacts (default: /var/lib/mlx/cache)
  --hf-home PATH      Hugging Face cache dir (default: <cache-dir>/huggingface)
  --user USER         Service user (default: mlx)
  --skip-prefetch     Do not prefetch model repos before starting the service
  --prefetch-only     Prefetch model repos and exit without restarting launchd
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model-path)
      MLX_MODEL_PATH="${2:-}"
      MODEL_PATH_FROM_CLI="true"
      shift 2
      ;;
    --model-type)
      MLX_MODEL_TYPE="${2:-}"
      MODEL_TYPE_FROM_CLI="true"
      shift 2
      ;;
    --config)
      MLX_CONFIG_PATH="${2:-}"
      CONFIG_PATH_FROM_CLI="true"
      shift 2
      ;;
    --host)
      MLX_HOST="${2:-}"
      HOST_FROM_CLI="true"
      shift 2
      ;;
    --port)
      MLX_PORT="${2:-}"
      PORT_FROM_CLI="true"
      shift 2
      ;;
    --cache-dir)
      XDG_CACHE_HOME="${2:-}"
      CACHE_HOME_FROM_CLI="true"
      shift 2
      ;;
    --hf-home)
      HF_HOME="${2:-}"
      HF_HOME_FROM_CLI="true"
      shift 2
      ;;
    --user)
      MLX_USER="${2:-}"
      shift 2
      ;;
    --skip-prefetch)
      PREFETCH_BEFORE_START="0"
      PREFETCH_FROM_CLI="true"
      shift
      ;;
    --prefetch-only)
      PREFETCH_BEFORE_START="1"
      PREFETCH_FROM_CLI="prefetch_only"
      shift
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

if [[ -f "$MLX_ENV_FILE" ]]; then
  if [[ "$HOST_FROM_SHELL" != "true" && "$HOST_FROM_CLI" != "true" ]]; then
    MLX_HOST="$(env_file_get "$MLX_ENV_FILE" MLX_HOST "$MLX_HOST")"
  fi
  if [[ "$PORT_FROM_SHELL" != "true" && "$PORT_FROM_CLI" != "true" ]]; then
    MLX_PORT="$(env_file_get "$MLX_ENV_FILE" MLX_PORT "$MLX_PORT")"
  fi
  if [[ "$MODEL_PATH_FROM_SHELL" != "true" && "$MODEL_PATH_FROM_CLI" != "true" ]]; then
    MLX_MODEL_PATH="$(env_file_get "$MLX_ENV_FILE" MLX_MODEL_PATH "$MLX_MODEL_PATH")"
  fi
  if [[ "$MODEL_TYPE_FROM_SHELL" != "true" && "$MODEL_TYPE_FROM_CLI" != "true" ]]; then
    MLX_MODEL_TYPE="$(env_file_get "$MLX_ENV_FILE" MLX_MODEL_TYPE "$MLX_MODEL_TYPE")"
  fi
  if [[ "$CONFIG_PATH_FROM_SHELL" != "true" && "$CONFIG_PATH_FROM_CLI" != "true" ]]; then
    MLX_CONFIG_PATH="$(env_file_get "$MLX_ENV_FILE" MLX_CONFIG_PATH "$MLX_CONFIG_PATH")"
  fi
  if [[ "$CACHE_HOME_FROM_SHELL" != "true" && "$CACHE_HOME_FROM_CLI" != "true" ]]; then
    XDG_CACHE_HOME="$(env_file_get "$MLX_ENV_FILE" XDG_CACHE_HOME "$XDG_CACHE_HOME")"
  fi
  if [[ "$HF_HOME_FROM_SHELL" != "true" && "$HF_HOME_FROM_CLI" != "true" ]]; then
    HF_HOME="$(env_file_get "$MLX_ENV_FILE" HF_HOME "$HF_HOME")"
  fi
  if [[ "$PREFETCH_FROM_CLI" != "true" && "$PREFETCH_FROM_CLI" != "prefetch_only" ]]; then
    PREFETCH_BEFORE_START="$(env_file_get "$MLX_ENV_FILE" PREFETCH_BEFORE_START "$PREFETCH_BEFORE_START")"
  fi
fi

if [[ ! "$MLX_PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: invalid --port value: ${MLX_PORT}" >&2
  exit 2
fi

case "$(lowercase_value "$PREFETCH_BEFORE_START")" in
  1|true|yes|on) PREFETCH_BEFORE_START="1" ;;
  0|false|no|off) PREFETCH_BEFORE_START="0" ;;
  *)
    echo "ERROR: invalid PREFETCH_BEFORE_START value: ${PREFETCH_BEFORE_START}" >&2
    exit 2
    ;;
esac

if [[ -n "$MLX_CONFIG_PATH" && ! -f "$MLX_CONFIG_PATH" ]]; then
  echo "ERROR: MLX config file not found: ${MLX_CONFIG_PATH}" >&2
  exit 2
fi

if [[ -z "${XDG_CACHE_HOME:-}" ]]; then
  echo "ERROR: XDG_CACHE_HOME must not be empty" >&2
  exit 2
fi

if [[ -z "${HF_HOME:-}" ]]; then
  echo "ERROR: HF_HOME must not be empty" >&2
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
require_cmd curl

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

sudo mkdir -p "${MLX_HOME}" "${MLX_HOME}/run" "${MLX_LOG_DIR}" "${XDG_CACHE_HOME}" "${HF_HOME}"
sudo chown -R "${MLX_USER}:staff" "${MLX_HOME}" "${MLX_LOG_DIR}" "${XDG_CACHE_HOME}" "${HF_HOME}"
sudo chmod 750 "${MLX_HOME}" "${MLX_HOME}/run" "${MLX_LOG_DIR}" "${XDG_CACHE_HOME}" "${HF_HOME}"

if [[ -x "${MLX_VENV}/bin/python" ]]; then
  if ! ns_python_is_at_least "${MLX_VENV}/bin/python" 3 11; then
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
sudo -H "${MLX_VENV}/bin/python" -m pip install --upgrade --no-cache-dir ${MLX_PIP_PACKAGES} huggingface_hub

if [[ ! -x "${MLX_VENV}/bin/mlx-openai-server" ]]; then
  echo "ERROR: mlx-openai-server executable was not installed into ${MLX_VENV}/bin" >&2
  exit 1
fi

sudo chown root:wheel "${MLX_VENV}/bin/mlx-openai-server" 2>/dev/null || true
sudo chmod 755 "${MLX_VENV}/bin/mlx-openai-server" 2>/dev/null || true

sudo cp "${ROOT_DIR}/services/mlx/scripts/run-native-macos.sh" "${MLX_LAUNCHER}"
sudo chown root:wheel "${MLX_LAUNCHER}"
sudo chmod 755 "${MLX_LAUNCHER}"
sudo cp "${ROOT_DIR}/services/mlx/scripts/prefetch-models.sh" "${MLX_PREFETCHER}"
sudo cp "${ROOT_DIR}/services/mlx/scripts/prefetch_models.py" "${MLX_PREFETCH_HELPER}"
sudo cp "${ROOT_DIR}/services/mlx/scripts/prefetch_models.py" "${MLX_PREFETCH_HELPER_COMPAT}"
sudo chown root:wheel "${MLX_PREFETCHER}" "${MLX_PREFETCH_HELPER}" "${MLX_PREFETCH_HELPER_COMPAT}"
sudo chmod 755 "${MLX_PREFETCHER}" "${MLX_PREFETCH_HELPER}" "${MLX_PREFETCH_HELPER_COMPAT}"

update_env_file_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "$file" ]]; then
    grep -v -E "^[[:space:]]*${key}=" "$file" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  sudo install -o root -g wheel -m 644 "$tmp" "$file"
  rm -f "$tmp"
}

update_env_file_key "${MLX_ENV_FILE}" MLX_HOST "${MLX_HOST}"
update_env_file_key "${MLX_ENV_FILE}" MLX_PORT "${MLX_PORT}"
update_env_file_key "${MLX_ENV_FILE}" MLX_MODEL_PATH "${MLX_MODEL_PATH}"
update_env_file_key "${MLX_ENV_FILE}" MLX_MODEL_TYPE "${MLX_MODEL_TYPE}"
update_env_file_key "${MLX_ENV_FILE}" MLX_CONFIG_PATH "${MLX_CONFIG_PATH}"
update_env_file_key "${MLX_ENV_FILE}" XDG_CACHE_HOME "${XDG_CACHE_HOME}"
update_env_file_key "${MLX_ENV_FILE}" HF_HOME "${HF_HOME}"
update_env_file_key "${MLX_ENV_FILE}" PREFETCH_BEFORE_START "${PREFETCH_BEFORE_START}"

if [[ "$PREFETCH_BEFORE_START" == "1" ]]; then
  echo "Prefetching MLX model repositories before starting launchd service..." >&2
  sudo -H -u "${MLX_USER}" env \
    HOME="${MLX_HOME}" \
    HF_HOME="${HF_HOME}" \
    XDG_CACHE_HOME="${XDG_CACHE_HOME}" \
    MLX_ENV_FILE="${MLX_ENV_FILE}" \
    MLX_VENV="${MLX_VENV}" \
    "${MLX_PREFETCHER}"
fi

if [[ "$PREFETCH_FROM_CLI" == "prefetch_only" ]]; then
  echo "Prefetch complete; skipping launchd restart because --prefetch-only was requested." >&2
  exit 0
fi

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
      <string>/bin/bash</string>
      <string>${MLX_LAUNCHER}</string>
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
      <string>${HF_HOME}</string>
      <key>XDG_CACHE_HOME</key>
      <string>${XDG_CACHE_HOME}</string>
      <key>MLX_ENV_FILE</key>
      <string>${MLX_ENV_FILE}</string>
      <key>MLX_VENV</key>
      <string>${MLX_VENV}</string>
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
sudo launchctl remove "${LAUNCHD_LABEL}" 2>/dev/null || true
sudo rm -f "${PLIST_PATH}.bak" 2>/dev/null || true
sudo cp "${PLIST_PATH}" "${PLIST_PATH}.bak"
if ! sudo launchctl bootstrap system "${PLIST_PATH}"; then
  echo "ERROR: launchctl bootstrap failed for ${LAUNCHD_LABEL}" >&2
  echo "Try these cleanup commands, then rerun installer:" >&2
  echo "  sudo launchctl bootout system/${LAUNCHD_LABEL} || true" >&2
  echo "  sudo launchctl remove ${LAUNCHD_LABEL} || true" >&2
  echo "  sudo rm -f ${PLIST_PATH}" >&2
  echo "Current state:" >&2
  echo "  sudo launchctl print system/${LAUNCHD_LABEL}" >&2
  exit 1
fi
sudo launchctl kickstart -k "system/${LAUNCHD_LABEL}"

for _ in {1..20}; do
  if curl -fsS "http://${MLX_HOST}:${MLX_PORT}/v1/models" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://${MLX_HOST}:${MLX_PORT}/v1/models" >/dev/null 2>&1; then
  echo "ERROR: launchd service started but health endpoint is not reachable yet: http://${MLX_HOST}:${MLX_PORT}/v1/models" >&2
  echo "Check service state:" >&2
  echo "  sudo launchctl print system/${LAUNCHD_LABEL}" >&2
  echo "Check logs:" >&2
  echo "  sudo tail -n 120 ${MLX_LOG_DIR}/mlx-openai.err.log" >&2
  echo "  sudo tail -n 120 ${MLX_LOG_DIR}/mlx-openai.out.log" >&2
  exit 1
fi

echo "Installed ${LAUNCHD_LABEL} (${MLX_USER}) on ${MLX_HOST}:${MLX_PORT}" >&2
echo "Health check: curl -fsS http://${MLX_HOST}:${MLX_PORT}/v1/models" >&2
echo "Runtime config: ${MLX_ENV_FILE} (edit and kickstart ${LAUNCHD_LABEL} to change model/path)" >&2
