#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
WITH_TELEGRAM="false"
EXTERNAL_VLLM="false"
EXTERNAL_VLLM_SET="false"
WITH_MLX="false"
EXTERNAL_MLX="false"
EXTERNAL_MLX_SET="false"
REMOVE_ORPHANS="false"
REMOVE_VOLUMES="false"
STOP_COLIMA="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/stop-stack.sh [--env-file PATH] [OPTIONS]

Bring down the Nexus core stack (gateway + etcd + optional components).
Mirrors the compose-file selection logic of ops-stack.sh so it tears down
exactly what ops-stack.sh would bring up.

Options:
  --env-file PATH     Env file path (default: ./.env)
  --external-vllm     Exclude docker-compose.vllm.yml (same as ops-stack.sh)
  --with-telegram     Include docker-compose.telegram-bot.yml
  --with-mlx          Include docker-compose.mlx.yml (containerised MLX)
  --external-mlx      Exclude docker-compose.mlx.yml (host-native MLX)
  --remove-orphans    Pass --remove-orphans to compose down
  --volumes           Also remove named volumes (WARNING: destroys persistent data)
  --stop-colima       Stop the Colima VM after compose down
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --external-vllm)
      EXTERNAL_VLLM="true"
      EXTERNAL_VLLM_SET="true"
      shift
      ;;
    --with-telegram)
      WITH_TELEGRAM="true"
      shift
      ;;
    --with-mlx)
      WITH_MLX="true"
      shift
      ;;
    --external-mlx)
      EXTERNAL_MLX="true"
      EXTERNAL_MLX_SET="true"
      shift
      ;;
    --remove-orphans)
      REMOVE_ORPHANS="true"
      shift
      ;;
    --volumes)
      REMOVE_VOLUMES="true"
      shift
      ;;
    --stop-colima)
      STOP_COLIMA="true"
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
  ns_print_warn "Env file not found at $ENV_FILE; continuing without it"
fi

# Auto-detect external vllm/mlx the same way ops-stack.sh does
if [[ "$EXTERNAL_VLLM_SET" != "true" && -f "$ENV_FILE" ]]; then
  vllm_base_url="$(ns_env_get "$ENV_FILE" VLLM_BASE_URL "http://host.docker.internal:8000/v1")"
  vllm_base_url="${vllm_base_url%/}"
  if [[ "$vllm_base_url" != "http://vllm:8000/v1" ]]; then
    EXTERNAL_VLLM="true"
  fi
fi

if [[ "$EXTERNAL_MLX_SET" != "true" && -f "$ENV_FILE" ]]; then
  mlx_base_url="$(ns_env_get "$ENV_FILE" MLX_BASE_URL "")"
  mlx_base_url="${mlx_base_url%/}"
  if [[ -n "$mlx_base_url" && "$mlx_base_url" != "http://mlx:10240/v1" ]]; then
    EXTERNAL_MLX="true"
  fi
fi

COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.etcd.yml)
if [[ "$EXTERNAL_VLLM" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.vllm.yml)
fi
if [[ "$WITH_TELEGRAM" == "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.telegram-bot.yml)
fi
if [[ "$WITH_MLX" == "true" && "$EXTERNAL_MLX" == "true" ]]; then
  ns_die "Use either --with-mlx (containerised MLX) or --external-mlx (host-native MLX), not both."
fi
if [[ "$WITH_MLX" == "true" && "$EXTERNAL_MLX" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.mlx.yml)
fi

if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is not reachable"
fi

DOWN_ARGS=()
if [[ "$REMOVE_ORPHANS" == "true" ]]; then
  DOWN_ARGS+=(--remove-orphans)
fi
if [[ "$REMOVE_VOLUMES" == "true" ]]; then
  DOWN_ARGS+=(--volumes)
fi

ns_print_header "Stopping Nexus core stack"
if [[ -f "$ENV_FILE" ]]; then
  ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" down "${DOWN_ARGS[@]}"
else
  ns_compose "${COMPOSE_ARGS[@]}" down "${DOWN_ARGS[@]}"
fi

if [[ "$STOP_COLIMA" == "true" ]]; then
  ns_print_header "Stopping Colima VM"
  colima_profile="${COLIMA_PROFILE:-default}"
  if ns_have_cmd colima; then
    colima stop --profile "$colima_profile" || ns_print_warn "colima stop failed (VM may already be stopped)"
  else
    ns_print_warn "colima not found in PATH; skipping VM stop"
  fi
fi

ns_print_ok "Nexus stack is down"
