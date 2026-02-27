#!/usr/bin/env bash
set -euo pipefail

# Next steps (typical):
#  - Deploy first: ./deploy/scripts/deploy.sh dev main (or quickstart.sh)
#  - Then run: ./deploy/scripts/verify-gateway.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd docker

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
TOKEN="${GATEWAY_BEARER_TOKEN:-}"
if [[ -z "${TOKEN}" && -f "${ENV_FILE}" ]]; then
  TOKEN="$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")"
fi

# SYNC-CHECK(core-compose-files): keep aligned with ops-stack.sh and cutover-one-way.sh.
COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml)
for compose_file in docker-compose.gateway.yml docker-compose.ollama.yml docker-compose.etcd.yml; do
  if [[ ! -f "$ROOT_DIR/$compose_file" ]]; then
    ns_die "Compose file not found: $ROOT_DIR/$compose_file (run from a complete Nexus checkout)."
  fi
done

if [[ -z "${TOKEN}" ]]; then
  ns_die "GATEWAY_BEARER_TOKEN is not set (set env var or put it in ${ENV_FILE})."
fi

gateway_port="${GATEWAY_PORT:-}"
obs_port="${OBSERVABILITY_PORT:-}"
if [[ -f "${ENV_FILE}" ]]; then
  gateway_port="${gateway_port:-$(ns_env_get "${ENV_FILE}" GATEWAY_PORT 8800)}"
  obs_port="${obs_port:-$(ns_env_get "${ENV_FILE}" OBSERVABILITY_PORT 8801)}"
fi

gateway_port="${gateway_port:-8800}"
obs_port="${obs_port:-8801}"

if ! ns_compose_available; then
  ns_die "Docker Compose is not available (need either 'docker compose' or 'docker-compose')."
fi

ns_print_header "Gateway Verifier (in-container)"

if ! ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" ps gateway >/dev/null 2>&1; then
  ns_die "Compose could not resolve service 'gateway'. Ensure core compose files are present and try: ./deploy/scripts/ops-stack.sh"
fi

# Run the verifier inside the gateway container so we don't depend on host Python.
ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T gateway \
  python3 /var/lib/gateway/tools/verify_gateway.py \
  --skip-pytest \
  --base-url "http://127.0.0.1:${gateway_port}" \
  --obs-url "http://127.0.0.1:${obs_port}" \
  --token "${TOKEN}"

ns_print_ok "Verifier passed"
