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
Usage: deploy/scripts/restart-gateway.sh [--env-file PATH] [--no-build]

Restart only the Gateway service with the core control-plane compose files
so Gateway picks up updated code and runtime config (e.g. model_aliases.json).

Options:
  --env-file PATH   Env file path (default: ./.env)
  --no-build        Skip image rebuild (recreate gateway from the existing image)

If the optional nginx TLS proxy is running, this script also refreshes nginx
after recreating gateway so proxying does not keep a stale container IP.
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

ns_print_header "Restarting Gateway"
if ! ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f docker-compose.etcd.yml config >/dev/null 2>&1; then
  ns_print_error "Compose failed to parse $ENV_FILE"
  ns_print_warn "Check for malformed variable syntax (for example an unmatched \\${...} expression)."
  ns_print_warn "Hint: inspect around the line number reported by docker compose."
  exit 1
fi

if [[ "$NO_BUILD" == "true" ]]; then
  ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d --force-recreate gateway
else
  ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f docker-compose.etcd.yml up -d --build --force-recreate gateway
fi

if [[ -f "$ROOT_DIR/docker-compose.nginx.yml" ]]; then
  if ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f docker-compose.nginx.yml ps nginx >/dev/null 2>&1; then
    ns_print_header "Refreshing nginx proxy"
    ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f docker-compose.nginx.yml up -d --force-recreate nginx
  fi
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
    exit 0
  fi
  sleep 2
done

ns_print_error "Gateway did not become healthy in time (${obs_health_url})"
ns_compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml ps || true
ns_compose -f docker-compose.gateway.yml -f docker-compose.etcd.yml logs --tail=120 gateway || true
exit 1
