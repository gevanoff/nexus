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
  TOKEN="$(grep -E '^GATEWAY_BEARER_TOKEN=' "${ENV_FILE}" | head -n 1 | cut -d '=' -f2-)"
fi

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

# Run the verifier inside the gateway container so we don't depend on host Python.
ns_compose exec -T gateway \
  python3 /var/lib/gateway/tools/verify_gateway.py \
  --skip-pytest \
  --base-url "http://127.0.0.1:${gateway_port}" \
  --obs-url "http://127.0.0.1:${obs_port}" \
  --token "${TOKEN}"

ns_print_ok "Verifier passed"
