#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
NO_BUILD="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/redeploy-tts-shims.sh [--env-file PATH] [--no-build]

Redeploy containerized Pocket TTS, LuxTTS, and Qwen3-TTS services on their
already assigned topology host. This command never starts or restarts Gateway.

Options:
  --env-file PATH   Env file path (default: ./.env)
  --no-build        Skip image rebuild (use compose up -d)
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
    -h | --help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  ns_die "Env file not found: $ENV_FILE"
fi

if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is not reachable"
fi

ns_print_header "Preparing runtime dirs"
host_runtime_root="$(ns_runtime_root_from_env "$ROOT_DIR" "$ENV_FILE")"
export NEXUS_RUNTIME_ROOT
NEXUS_RUNTIME_ROOT="$(ns_resolve_docker_bind_path "$host_runtime_root")"
ns_ensure_runtime_dirs "$ROOT_DIR"

compose_args=(
  --env-file "$ENV_FILE"
  -f docker-compose.tts.yml
  -f docker-compose.luxtts.yml
  -f docker-compose.qwen3-tts.yml
)

ns_print_header "Redeploying Pocket TTS + LuxtTS + Qwen3-TTS"
if [[ "$NO_BUILD" == "true" ]]; then
  ns_compose "${compose_args[@]}" up -d tts luxtts luxtts-registrar qwen3-tts qwen3-tts-registrar
else
  ns_compose "${compose_args[@]}" up -d --build tts luxtts luxtts-registrar qwen3-tts qwen3-tts-registrar
fi

ns_print_header "Waiting for TTS service health"
tts_port="$(ns_env_get "$ENV_FILE" TTS_PORT 9940)"
luxtts_port="$(ns_env_get "$ENV_FILE" LUXTTS_PORT 9170)"
qwen3_tts_port="$(ns_env_get "$ENV_FILE" QWEN3_TTS_PORT 9175)"
for i in {1..60}; do
  pocket_ok="false"
  luxtts_ok="false"
  qwen_ok="false"

  if curl -fsS "http://127.0.0.1:${tts_port}/health" >/dev/null 2>&1; then
    pocket_ok="true"
  fi
  if curl -fsS "http://127.0.0.1:${luxtts_port}/health" >/dev/null 2>&1; then
    luxtts_ok="true"
  fi
  if curl -fsS "http://127.0.0.1:${qwen3_tts_port}/health" >/dev/null 2>&1; then
    qwen_ok="true"
  fi

  if [[ "$pocket_ok" == "true" && "$luxtts_ok" == "true" && "$qwen_ok" == "true" ]]; then
    ns_print_ok "Pocket TTS, LuxtTS, and Qwen3-TTS health endpoints are up"
    break
  fi

  if [[ "$i" -eq 60 ]]; then
    ns_print_error "Timed out waiting for TTS service health endpoints"
    ns_compose "${compose_args[@]}" ps || true
    ns_compose "${compose_args[@]}" logs --tail=120 tts luxtts luxtts-registrar qwen3-tts qwen3-tts-registrar || true
    exit 1
  fi
  sleep 2
done

ns_print_header "TTS service redeploy complete"
ns_compose "${compose_args[@]}" ps
