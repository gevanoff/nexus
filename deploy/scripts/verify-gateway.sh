#!/usr/bin/env bash
set -euo pipefail

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

if ! docker compose version >/dev/null 2>&1; then
  ns_die "Docker Compose plugin not available (need: docker compose)."
fi

ns_print_header "Gateway Verifier (in-container)"

# Run the verifier inside the gateway container so we don't depend on host Python.
docker compose exec -T gateway \
  python3 /var/lib/gateway/tools/verify_gateway.py \
  --skip-pytest \
  --base-url http://127.0.0.1:8800 \
  --obs-url http://127.0.0.1:8801 \
  --token "${TOKEN}"

ns_print_ok "Verifier passed"
