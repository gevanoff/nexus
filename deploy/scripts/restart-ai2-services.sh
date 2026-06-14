#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

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

export GATEWAY_ENV_FILE
GATEWAY_ENV_FILE="$(ns_resolve_docker_env_file "$ENV_FILE")"

ns_print_header "Restoring ai2 services"
"$ROOT_DIR/deploy/scripts/ops-stack.sh" --env-file "$ENV_FILE" --no-pull --no-build
"$ROOT_DIR/deploy/scripts/redeploy-tts-shims.sh" --env-file "$ENV_FILE" --no-build --skip-gateway
