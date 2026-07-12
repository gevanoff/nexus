#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

MODEL=""
DRY_RUN="false"
TIMEOUT_SEC="${MLX_HUGE_SWITCH_TIMEOUT_SEC:-3600}"

if [[ -z "${MLX_NATIVE_ROOT:-}" ]]; then
  if [[ ! -e /var/lib/mlx && -d /ai-data/var/lib/mlx ]]; then
    MLX_NATIVE_ROOT="/ai-data/var/lib/mlx"
  else
    MLX_NATIVE_ROOT="/var/lib/mlx"
  fi
fi

usage() {
  cat <<'EOF'
Usage: deploy/scripts/switch-mlx-huge-model.sh --model ORG/REPO [--dry-run] [--timeout-sec N]

Guardedly replace the single resident MLX Huge model, restart MLX, and verify it.
The previous runtime config is restored automatically when activation fails.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
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

if [[ ! "$TIMEOUT_SEC" =~ ^[0-9]+$ ]] || (( TIMEOUT_SEC < 600 )); then
  ns_die "--timeout-sec must be an integer >= 600"
fi

MODEL_TYPE="lm"
WIRED_LIMIT_MB=""
TOOL_CALL_PARSER=""
REASONING_PARSER=""
case "$MODEL" in
  mlx-community/GLM-5.2-4bit)
    WIRED_LIMIT_MB="450000"
    TOOL_CALL_PARSER="glm4_moe"
    REASONING_PARSER="glm4_moe"
    ;;
  mlx-community/DeepSeek-R1-0528-4bit)
    WIRED_LIMIT_MB="430000"
    REASONING_PARSER="qwen3"
    ;;
  *)
    ns_die "Model is not approved for the resident MLX Huge lane: $MODEL"
    ;;
esac

MLX_ENV_FILE="${MLX_ENV_FILE:-${MLX_NATIVE_ROOT}/mlx.env}"
MLX_CONFIG_PATH="${MLX_CONFIG_PATH:-$(ns_env_get "$MLX_ENV_FILE" MLX_CONFIG_PATH "${MLX_NATIVE_ROOT}/config/config.yaml")}"
HF_HOME="${HF_HOME:-$(ns_env_get "$MLX_ENV_FILE" HF_HOME "/ai-data/huggingface")}"
CACHE_PATH="${HF_HOME}/models--${MODEL//\//--}"
LOCK_DIR="${MLX_NATIVE_ROOT}/locks/huge-lane-switch.lock"
SYSCTL_PLIST="/Library/LaunchDaemons/com.nexus.mlx.wired-limit.plist"

[[ -d "$CACHE_PATH" ]] || ns_die "Resident model cache is missing: $CACHE_PATH"
if find "$CACHE_PATH" -type f -name '*.incomplete' -print -quit | grep -q .; then
  ns_die "Resident model cache is incomplete: $CACHE_PATH"
fi
snapshot_ref="$(head -n 1 "$CACHE_PATH/refs/main" 2>/dev/null | tr -d '\r\n' || true)"
snapshot_path="$CACHE_PATH/snapshots/$snapshot_ref"
[[ -n "$snapshot_ref" && -d "$snapshot_path" ]] || ns_die "Resident model snapshot is missing: $CACHE_PATH"
if ! python3 "$ROOT_DIR/services/mlx/scripts/verify_model_snapshot.py" "$snapshot_path"; then
  ns_die "Resident model cache has missing numbered shards: $CACHE_PATH"
fi

mkdir -p "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  ns_die "Another MLX Huge transition is already active: $LOCK_DIR"
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "model=$MODEL"
echo "wired_limit_mb=$WIRED_LIMIT_MB"
echo "config_path=$MLX_CONFIG_PATH"
if [[ "$DRY_RUN" == "true" ]]; then
  echo "dry_run=true"
  exit 0
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
config_backup="${MLX_CONFIG_PATH}.pre-switch-${timestamp}"
env_backup="${MLX_ENV_FILE}.pre-switch-${timestamp}"
config_tmp="$(mktemp "${MLX_CONFIG_PATH}.tmp.XXXXXX")"
plist_tmp="$(mktemp /tmp/com.nexus.mlx.wired-limit.XXXXXX.plist)"
trap 'rm -f "$config_tmp" "$plist_tmp"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cp "$MLX_CONFIG_PATH" "$config_backup"
cp "$MLX_ENV_FILE" "$env_backup"
previous_wired_limit_mb="$(ns_env_get "$env_backup" MLX_WIRED_LIMIT_MB "$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)")"

{
  cat <<EOF
server:
  host: 0.0.0.0
  port: 10240

models:
  - served_model_name: mlx-community/bge-small-en-v1.5-8bit
    model_path: mlx-community/bge-small-en-v1.5-8bit
    model_type: embeddings

  - served_model_name: ${MODEL}
    model_path: ${MODEL}
    model_type: ${MODEL_TYPE}
    on_demand: false
EOF
  [[ -n "$TOOL_CALL_PARSER" ]] && printf '    tool_call_parser: %s\n' "$TOOL_CALL_PARSER"
  [[ -n "$REASONING_PARSER" ]] && printf '    reasoning_parser: %s\n' "$REASONING_PARSER"
  printf '    default_max_tokens: 2048\n'
} >"$config_tmp"

update_env_key() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp "${MLX_ENV_FILE}.tmp.XXXXXX")"
  awk -F= -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    $1 == key { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$MLX_ENV_FILE" >"$tmp"
  mv "$tmp" "$MLX_ENV_FILE"
}

update_env_key MLX_MODEL_READY_TIMEOUT_SEC "$TIMEOUT_SEC"
update_env_key MLX_WIRED_LIMIT_MB "$WIRED_LIMIT_MB"
update_env_key MLX_RESIDENT_HUGE_MODEL "$MODEL"
sudo -n chown mlx:staff "$MLX_ENV_FILE"
sudo -n chmod 664 "$MLX_ENV_FILE"

sudo -n install -o root -g wheel -m 644 "$config_tmp" "$MLX_CONFIG_PATH"

install_wired_limit() {
  local limit_mb="$1"
  cat >"$plist_tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.nexus.mlx.wired-limit</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/sbin/sysctl</string>
      <string>-w</string>
      <string>iogpu.wired_limit_mb=${limit_mb}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
  </dict>
</plist>
EOF
  sudo -n install -o root -g wheel -m 644 "$plist_tmp" "$SYSCTL_PLIST"
  sudo -n plutil -lint "$SYSCTL_PLIST" >/dev/null
  sudo -n launchctl bootout system/com.nexus.mlx.wired-limit 2>/dev/null || true
  sudo -n launchctl bootstrap system "$SYSCTL_PLIST"
  sudo -n launchctl kickstart -k system/com.nexus.mlx.wired-limit
  sudo -n sysctl -w "iogpu.wired_limit_mb=${limit_mb}" >/dev/null
}

install_wired_limit "$WIRED_LIMIT_MB"

rollback() {
  echo "activation_failed=true" >&2
  sudo -n install -o root -g wheel -m 644 "$config_backup" "$MLX_CONFIG_PATH"
  cp "$env_backup" "$MLX_ENV_FILE"
  install_wired_limit "$previous_wired_limit_mb"
  MLX_NATIVE_ROOT="$MLX_NATIVE_ROOT" "$ROOT_DIR/deploy/scripts/restart-mlx.sh" --timeout-sec "$TIMEOUT_SEC" || true
}

if ! MLX_NATIVE_ROOT="$MLX_NATIVE_ROOT" "$ROOT_DIR/deploy/scripts/restart-mlx.sh" --timeout-sec "$TIMEOUT_SEC"; then
  rollback
  ns_die "MLX failed to start with resident model $MODEL; previous config restored"
fi

models_json="$(curl -fsS http://127.0.0.1:10240/v1/models)"
if ! MODEL_JSON="$models_json" MODEL="$MODEL" python3 -c 'import json, os, sys; data=json.loads(os.environ["MODEL_JSON"]); ids={str(x.get("id") or "") for x in data.get("data", [])}; sys.exit(0 if os.environ["MODEL"] in ids else 1)'; then
  rollback
  ns_die "MLX started without advertising the selected resident model $MODEL; previous config restored"
fi

echo "resident_model=$MODEL"
echo "dry_run=false"
