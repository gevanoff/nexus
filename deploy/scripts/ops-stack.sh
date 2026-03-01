#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
BRANCH=""
NO_PULL="false"
NO_BUILD="false"
WITH_TELEGRAM="false"
WITH_MLX="false"
EXTERNAL_OLLAMA="false"
EXTERNAL_MLX="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/ops-stack.sh [--env-file PATH] [--branch BRANCH] [--no-pull] [--no-build]

Host-local daily operations helper for Nexus core stack:
  1) (optional) git pull
  2) ensure Docker daemon
  3) restart core containers (gateway + ollama + etcd)
  4) run gateway verifier

Options:
  --env-file PATH   Env file path (default: ./.env)
  --branch BRANCH   If set, checkout+pull this branch before restart
  --no-pull         Skip git fetch/pull
  --no-build        Skip image rebuild (use compose up -d without --build)
  --with-telegram   Include telegram-bot component (docker-compose.telegram-bot.yml)
  --with-mlx        Include MLX component (docker-compose.mlx.yml)
  --external-ollama Use external/native Ollama (do not include docker-compose.ollama.yml)
  --external-mlx    Use external/native MLX (do not include docker-compose.mlx.yml)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --no-pull)
      NO_PULL="true"
      shift
      ;;
    --no-build)
      NO_BUILD="true"
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
    --external-ollama)
      EXTERNAL_OLLAMA="true"
      shift
      ;;
    --external-mlx)
      EXTERNAL_MLX="true"
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

COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.etcd.yml)
if [[ "$EXTERNAL_OLLAMA" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.ollama.yml)
fi
if [[ "$WITH_TELEGRAM" == "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.telegram-bot.yml)
fi
if [[ "$WITH_MLX" == "true" && "$EXTERNAL_MLX" == "true" ]]; then
  ns_die "Use either --with-mlx (containerized MLX) or --external-mlx (host-native MLX), not both."
fi
if [[ "$WITH_MLX" == "true" && "$EXTERNAL_MLX" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.mlx.yml)
fi

ns_print_header "Nexus Ops: update + restart + verify"

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

ns_ensure_project_env_bind_source "$ROOT_DIR" "$ENV_FILE"

if [[ "$NO_PULL" != "true" ]]; then
  ns_print_header "Updating code"
  if ! ns_have_cmd git; then
    ns_die "git is required for update step"
  fi

  if [[ -n "$BRANCH" ]]; then
    if [[ ! "$BRANCH" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
      ns_die "Invalid branch name: $BRANCH"
    fi
    git fetch origin "$BRANCH"
    git checkout "$BRANCH"
    git pull --ff-only origin "$BRANCH"
  else
    current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [[ -n "$current_branch" ]]; then
      git fetch origin "$current_branch"
      git pull --ff-only origin "$current_branch"
    else
      ns_print_warn "Could not detect current branch; skipping pull"
    fi
  fi
fi

ns_print_header "Ensuring Docker runtime"
ns_ensure_prereqs true true false false false false || true
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is not reachable"
fi
if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi

ns_print_header "Preparing runtime"
ns_ensure_runtime_dirs "$ROOT_DIR"
ns_seed_gateway_config_files "$ROOT_DIR"
ns_verify_docker_bind_source "$ROOT_DIR"
ns_verify_docker_bind_source "$ROOT_DIR/.env"

ns_print_header "Restarting core stack"
if [[ "$NO_BUILD" == "true" ]]; then
  ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" up -d
else
  ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" up -d --build
fi

ns_print_header "Waiting for gateway health"
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
  sleep 2
  if [[ "$i" -eq 60 ]]; then
    ns_print_error "Gateway did not become healthy in time (${obs_health_url})"
    ns_compose "${COMPOSE_ARGS[@]}" ps || true
    ns_compose "${COMPOSE_ARGS[@]}" logs --tail=120 gateway || true
    exit 1
  fi
done

ns_print_header "Running verifier"
if [[ "$WITH_MLX" == "true" ]]; then
  if [[ "$EXTERNAL_OLLAMA" == "true" ]]; then
    ENV_FILE="$ENV_FILE" "$ROOT_DIR/deploy/scripts/verify-gateway.sh" --with-mlx --external-ollama
  else
    ENV_FILE="$ENV_FILE" "$ROOT_DIR/deploy/scripts/verify-gateway.sh" --with-mlx
  fi
else
  verify_args=()
  if [[ "$EXTERNAL_OLLAMA" == "true" ]]; then
    verify_args+=(--external-ollama)
  fi
  if [[ "$EXTERNAL_MLX" == "true" ]]; then
    verify_args+=(--external-mlx)
  fi
  ENV_FILE="$ENV_FILE" "$ROOT_DIR/deploy/scripts/verify-gateway.sh" "${verify_args[@]}"
fi

ns_print_header "Ops complete"
ns_compose "${COMPOSE_ARGS[@]}" ps
