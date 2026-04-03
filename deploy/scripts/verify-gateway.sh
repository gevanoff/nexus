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
EXTERNAL_VLLM="false"
EXTERNAL_VLLM_SET="false"
WITH_MLX="false"
EXTERNAL_MLX="false"
EXTERNAL_MLX_SET="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/verify-gateway.sh [--env-file PATH] [--external-vllm] [--with-mlx] [--external-mlx]

Run in-container gateway contract verification.

Options:
  --env-file PATH   Env file path (default: ./.env)
  --external-vllm   Use external/native vLLM (do not include docker-compose.vllm.yml).
                    If not set explicitly, auto-detected from VLLM_BASE_URL.
  --with-mlx        Include legacy MLX compose component (docker-compose.mlx.yml) in compose checks
  --external-mlx    Use external/native MLX (do not include docker-compose.mlx.yml).
                     If not set explicitly, auto-detected from MLX_BASE_URL.
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
    --with-mlx)
      WITH_MLX="true"
      shift
      ;;
    --external-mlx)
      EXTERNAL_MLX="true"
      EXTERNAL_MLX_SET="true"
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

if [[ ! -f "${ENV_FILE}" ]]; then
  ns_print_warn "Env file not found at ${ENV_FILE}; creating from .env.example"
  ns_ensure_env_file "${ENV_FILE}" "$ROOT_DIR"
fi

if [[ "$EXTERNAL_VLLM_SET" != "true" ]]; then
  vllm_base_url="$(ns_env_get "${ENV_FILE}" VLLM_BASE_URL "http://host.docker.internal:8000/v1")"
  vllm_base_url="${vllm_base_url%/}"
  if [[ "$vllm_base_url" != "http://vllm:8000/v1" ]]; then
    EXTERNAL_VLLM="true"
  fi
fi

if [[ "$EXTERNAL_MLX_SET" != "true" ]]; then
  mlx_base_url="$(ns_env_get "${ENV_FILE}" MLX_BASE_URL "")"
  mlx_base_url="${mlx_base_url%/}"
  if [[ -n "$mlx_base_url" && "$mlx_base_url" != "http://mlx:10240/v1" ]]; then
    EXTERNAL_MLX="true"
  fi
fi

if [[ -z "${TOKEN}" && -f "${ENV_FILE}" ]]; then
  TOKEN="$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")"
fi

# SYNC-CHECK(core-compose-files): keep aligned with ops-stack.sh and cutover-one-way.sh.
COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.etcd.yml)
COMPOSE_FILES=(docker-compose.gateway.yml docker-compose.etcd.yml)
if [[ "$EXTERNAL_VLLM" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.vllm.yml)
  COMPOSE_FILES+=(docker-compose.vllm.yml)
fi
if [[ "$WITH_MLX" == "true" && "$EXTERNAL_MLX" == "true" ]]; then
  ns_die "Use either --with-mlx (containerized MLX) or --external-mlx (host-native MLX), not both."
fi
if [[ "$WITH_MLX" == "true" && "$EXTERNAL_MLX" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.mlx.yml)
  COMPOSE_FILES+=(docker-compose.mlx.yml)
fi
for compose_file in "${COMPOSE_FILES[@]}"; do
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
  ns_die "Compose could not resolve service 'gateway'. Ensure compose files are present and try: ./deploy/scripts/ops-stack.sh"
fi

# Run the verifier inside the gateway container so we don't depend on host Python.
ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T gateway \
  python3 /var/lib/gateway/tools/verify_gateway.py \
  --skip-pytest \
  --base-url "http://127.0.0.1:${gateway_port}" \
  --obs-url "http://127.0.0.1:${obs_port}" \
  --token "${TOKEN}"

ns_print_ok "Verifier passed"
