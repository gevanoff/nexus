#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/deploy-cloudflared.sh [--env-file PATH]

Deploy the remotely managed Cloudflare Tunnel connector beside Gateway.
The selected environment file must contain CLOUDFLARED_TUNNEL_TOKEN.

Default env-file order:
  1. deploy/env/.env.prod.ai2
  2. deploy/env/.env.prod
  3. ./.env
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
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

if [[ -z "$ENV_FILE" ]]; then
  for candidate in \
    "$ROOT_DIR/deploy/env/.env.prod.ai2" \
    "$ROOT_DIR/deploy/env/.env.prod" \
    "$ROOT_DIR/.env"; do
    if [[ -f "$candidate" ]]; then
      ENV_FILE="$candidate"
      break
    fi
  done
fi

if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  ns_die "No deployment env file found. Pass --env-file PATH."
fi

tunnel_token="$(ns_env_get "$ENV_FILE" CLOUDFLARED_TUNNEL_TOKEN "")"
if [[ -z "$tunnel_token" ]]; then
  ns_die "CLOUDFLARED_TUNNEL_TOKEN is missing from $ENV_FILE"
fi

if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is not reachable"
fi

# Gateway bind-mounts a project env file. Keep it synchronized with the selected
# deployment env before recreating Gateway with the tunnel-network attachment.
ns_ensure_project_env_bind_source "$ROOT_DIR" "$ENV_FILE" refresh
export GATEWAY_ENV_FILE
GATEWAY_ENV_FILE="$(ns_resolve_docker_env_file "$ROOT_DIR/.env")"

host_runtime_root="$(ns_runtime_root_from_env "$ROOT_DIR" "$ENV_FILE")"
export NEXUS_RUNTIME_ROOT
NEXUS_RUNTIME_ROOT="$(ns_resolve_docker_bind_path "$host_runtime_root")"

# cloudflared supports token files for remotely managed tunnels. Materialize the
# token under the protected runtime root so it is not exposed through the
# container environment or process arguments.
token_dir="$host_runtime_root/cloudflared"
token_path="$token_dir/tunnel-token"
token_tmp="$token_dir/.tunnel-token.$$"
mkdir -p "$token_dir"
chmod 700 "$token_dir"
printf '%s' "$tunnel_token" > "$token_tmp"
chmod 600 "$token_tmp"
mv -f "$token_tmp" "$token_path"
unset tunnel_token token_tmp

compose_args=(
  --env-file "$ENV_FILE"
  -f docker-compose.gateway.yml
  -f docker-compose.etcd.yml
  -f docker-compose.cloudflared.yml
)

ns_print_header "Validating Cloudflare Tunnel deployment"
GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" \
  ns_compose "${compose_args[@]}" config >/dev/null

ns_print_header "Pulling cloudflared"
GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" \
  ns_compose "${compose_args[@]}" pull cloudflared

ns_print_header "Ensuring etcd"
GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" \
  ns_compose "${compose_args[@]}" up -d etcd

ns_print_header "Attaching Gateway and starting Cloudflare Tunnel"
GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" \
  ns_compose "${compose_args[@]}" up -d --build --force-recreate gateway cloudflared

ns_print_header "Waiting for Gateway health"
obs_port="$(ns_env_get "$ENV_FILE" OBSERVABILITY_PORT "8801")"
for _ in {1..60}; do
  if curl -fsS "http://127.0.0.1:${obs_port}/health" >/dev/null 2>&1; then
    ns_print_ok "Gateway is healthy"
    break
  fi
  sleep 2
done

if ! curl -fsS "http://127.0.0.1:${obs_port}/health" >/dev/null 2>&1; then
  ns_print_error "Gateway did not become healthy"
  GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
  NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" \
    ns_compose "${compose_args[@]}" logs --tail=120 gateway cloudflared || true
  exit 1
fi

ns_print_header "Cloudflare Tunnel status"
GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" \
NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" \
  ns_compose "${compose_args[@]}" ps gateway cloudflared

cat <<'EOF'

Next: in Cloudflare Zero Trust, configure the remotely managed tunnel hostname
nexus.shadowrepository.org to use this origin service:

  http://nexus-gateway-tunnel:8800

Then protect the hostname with Cloudflare Access and create a more-specific
Bypass application only for /social-media/*.
EOF
