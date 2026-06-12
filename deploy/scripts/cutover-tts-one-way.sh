#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
NO_BUILD="false"
SKIP_GATEWAY="false"

LEGACY_TTS_LABELS=(
  com.nexus.pocket-tts.server
  com.nexus.luxtts.server
  com.nexus.qwen3-tts.server
)

usage() {
  cat <<'EOF'
Usage: deploy/scripts/cutover-tts-one-way.sh [--env-file PATH] [--no-build] [--skip-gateway]

One-way host-local cutover from legacy native Pocket TTS, LuxTTS, and Qwen3-TTS
launchd services to the tracked Nexus containerized TTS shims.

Options:
  --env-file PATH   Env file path for the Nexus compose redeploy (default: ./.env)
  --no-build        Skip image rebuild (use compose up -d)
  --skip-gateway    Do not restart gateway after redeploying TTS shims
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --no-build)
      NO_BUILD="true"
      shift
      ;;
    --skip-gateway)
      SKIP_GATEWAY="true"
      shift
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

if [[ "$(ns_detect_platform)" == "macos" ]] && [[ "${EUID:-$(id -u)}" -eq 0 ]] && ns_have_cmd colima; then
  ns_die "Do not run this script with sudo on macOS when using Colima. Run as a normal user and let individual commands use sudo."
fi

sync_dir_if_present() {
  local src="$1"
  local dst="$2"
  local label="$3"

  if [[ ! -d "$src" ]]; then
    ns_print_warn "Legacy ${label} source not found: $src"
    return 0
  fi

  ns_mkdir_p "$dst"
  if ns_have_cmd rsync; then
    rsync -a "$src/" "$dst/"
  else
    cp -R "$src/." "$dst/"
  fi
  ns_print_ok "Seeded ${label}: $src -> $dst"
}

stop_legacy_tts_launchd() {
  local label plist

  ns_print_header "Stopping legacy native TTS launchd services"
  for label in "${LEGACY_TTS_LABELS[@]}"; do
    plist="/Library/LaunchDaemons/${label}.plist"
    sudo launchctl bootout "system/${label}" 2>/dev/null || true
    sudo launchctl disable "system/${label}" 2>/dev/null || true
    sudo launchctl remove "${label}" 2>/dev/null || true
    sudo rm -f "$plist"
    ns_print_ok "Disabled legacy launchd job: ${label}"
  done

  pkill -f 'pocket_tts_server:app' 2>/dev/null || true
  pkill -f 'lux_tts_server:app' 2>/dev/null || true
  pkill -f 'qwen3_tts_server:app' 2>/dev/null || true
  pkill -f '/ai-data/var/lib/pocket-tts' 2>/dev/null || true
  pkill -f '/ai-data/var/lib/luxtts' 2>/dev/null || true
  pkill -f '/ai-data/var/lib/qwen3-tts' 2>/dev/null || true
}

seed_tts_runtime() {
  local runtime_root

  runtime_root="$(ns_runtime_root "$ROOT_DIR")"
  ns_print_header "Seeding Nexus TTS runtime from legacy host-native state"
  ns_ensure_runtime_dirs "$ROOT_DIR"

  sync_dir_if_present "/ai-data/var/lib/qwen3-tts/app" "${runtime_root}/qwen3-tts/data/app" "Qwen3-TTS app"
  sync_dir_if_present "/ai-data/var/lib/qwen3-tts/voices" "${runtime_root}/qwen3-tts/data/voices" "Qwen3-TTS voices"
  sync_dir_if_present "/ai-data/var/lib/luxtts/app" "${runtime_root}/luxtts/data/app" "LuxTTS app"
  sync_dir_if_present "/ai-data/var/lib/luxtts/voices" "${runtime_root}/luxtts/data/voices" "LuxTTS voices"
  sync_dir_if_present "/ai-data/var/lib/tts_refs" "${runtime_root}/tts_refs" "shared TTS refs"
}

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

stop_legacy_tts_launchd
seed_tts_runtime

redeploy_args=(--env-file "$ENV_FILE")
if [[ "$NO_BUILD" == "true" ]]; then
  redeploy_args+=(--no-build)
fi
if [[ "$SKIP_GATEWAY" == "true" ]]; then
  redeploy_args+=(--skip-gateway)
fi

ns_print_header "Redeploying tracked Nexus TTS shims"
"$ROOT_DIR/deploy/scripts/redeploy-tts-shims.sh" "${redeploy_args[@]}"