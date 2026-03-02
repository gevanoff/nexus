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

usage() {
  cat <<'EOF'
Usage: deploy/scripts/redeploy-tts-shims.sh [--env-file PATH] [--no-build] [--skip-gateway]

Redeploy containerized LuxtTS and Qwen3-TTS shims and optionally restart Gateway
so backend routing/health reflects updated TTS components.

Options:
  --env-file PATH   Env file path (default: ./.env)
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

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

ns_ensure_project_env_bind_source "$ROOT_DIR" "$ENV_FILE"

if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is not reachable"
fi

ns_print_header "Preparing runtime dirs"
ns_ensure_runtime_dirs "$ROOT_DIR"

compose_args=(
  --env-file "$ENV_FILE"
  -f docker-compose.gateway.yml
  -f docker-compose.etcd.yml
  -f docker-compose.luxtts.yml
  -f docker-compose.qwen3-tts.yml
)

ns_print_header "Redeploying LuxtTS + Qwen3-TTS"
if [[ "$NO_BUILD" == "true" ]]; then
  ns_compose "${compose_args[@]}" up -d luxtts qwen3-tts
else
  ns_compose "${compose_args[@]}" up -d --build luxtts qwen3-tts
fi

ns_print_header "Waiting for TTS shim health"
for i in {1..60}; do
  luxtts_ok="false"
  qwen_ok="false"

  if curl -fsS "http://127.0.0.1:${LUXTTS_PORT:-9170}/health" >/dev/null 2>&1; then
    luxtts_ok="true"
  fi
  if curl -fsS "http://127.0.0.1:${QWEN3_TTS_PORT:-9175}/health" >/dev/null 2>&1; then
    qwen_ok="true"
  fi

  if [[ "$luxtts_ok" == "true" && "$qwen_ok" == "true" ]]; then
    ns_print_ok "LuxtTS and Qwen3-TTS health endpoints are up"
    break
  fi

  if [[ "$i" -eq 60 ]]; then
    ns_print_error "Timed out waiting for LuxtTS/Qwen3-TTS health endpoints"
    ns_compose "${compose_args[@]}" ps || true
    ns_compose "${compose_args[@]}" logs --tail=120 luxtts qwen3-tts || true
    exit 1
  fi
  sleep 2
done

if [[ "$SKIP_GATEWAY" != "true" ]]; then
  ns_print_header "Restarting Gateway"
  if [[ "$NO_BUILD" == "true" ]]; then
    ns_compose "${compose_args[@]}" up -d gateway
  else
    ns_compose "${compose_args[@]}" up -d --build gateway
  fi

  ns_print_header "Waiting for Gateway health"
  obs_port="${OBSERVABILITY_PORT:-}"
  if [[ -z "${obs_port}" && -f "$ENV_FILE" ]]; then
    obs_port="$(ns_env_get "$ENV_FILE" OBSERVABILITY_PORT 8801)"
  fi
  obs_port="${obs_port:-8801}"
  obs_health_url="http://127.0.0.1:${obs_port}/health"

  for i in {1..60}; do
    if curl -fsS "$obs_health_url" >/dev/null 2>&1; then
      ns_print_ok "Gateway observability health endpoint is up (${obs_health_url})"
      break
    fi
    if [[ "$i" -eq 60 ]]; then
      ns_print_error "Gateway did not become healthy in time (${obs_health_url})"
      ns_compose "${compose_args[@]}" ps || true
      ns_compose "${compose_args[@]}" logs --tail=120 gateway || true
      exit 1
    fi
    sleep 2
  done
fi

ns_print_header "TTS shim redeploy complete"
ns_compose "${compose_args[@]}" ps
