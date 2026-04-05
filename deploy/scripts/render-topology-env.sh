#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

TOPOLOGY_FILE="${ROOT_DIR}/deploy/topology/production.json"
TOPOLOGY_HOST=""
ENVIRONMENT="prod"
ENV_FILE=""
TEMPLATE_FILE="${ROOT_DIR}/.env.example"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/render-topology-env.sh --topology-host HOST
                                            [--topology-file PATH]
                                            [--environment dev|prod]
                                            [--env-file PATH]
                                            [--template PATH]

Materializes a host env file from the tracked topology manifest.

Defaults:
  --topology-file deploy/topology/production.json
  --env-file      deploy/env/.env.<environment>.<host>
  --template      .env.example
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topology-file)
      TOPOLOGY_FILE="${2:-}"
      shift 2
      ;;
    --topology-host)
      TOPOLOGY_HOST="${2:-}"
      shift 2
      ;;
    --environment)
      ENVIRONMENT="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --template)
      TEMPLATE_FILE="${2:-}"
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

[[ -n "${TOPOLOGY_HOST:-}" ]] || ns_die "--topology-host is required"

case "$ENVIRONMENT" in
  dev|prod)
    ;;
  *)
    ns_die "Unknown environment: $ENVIRONMENT"
    ;;
esac

if [[ -z "${ENV_FILE:-}" ]]; then
  ENV_FILE="$ROOT_DIR/deploy/env/.env.${ENVIRONMENT}.${TOPOLOGY_HOST}"
fi

PYTHON="$(ns_pick_python || true)"
[[ -n "${PYTHON:-}" ]] || ns_die "python3/python is required but not installed."

ns_print_header "Materializing topology env"
ns_print_ok "Topology file: ${TOPOLOGY_FILE}"
ns_print_ok "Topology host: ${TOPOLOGY_HOST}"
ns_print_ok "Output env: ${ENV_FILE}"

"$PYTHON" "$ROOT_DIR/deploy/scripts/topology_tool.py" render-env \
  --topology-file "$TOPOLOGY_FILE" \
  --host "$TOPOLOGY_HOST" \
  --template "$TEMPLATE_FILE" \
  --out "$ENV_FILE" >/dev/null

platform="$(ns_detect_platform)"
token="$(ns_env_get "$ENV_FILE" GATEWAY_BEARER_TOKEN "change-me-in-production")"
if [[ -z "${token:-}" || "$token" == "change-me-in-production" || "$token" == "your-secret-token-here" ]]; then
  new_token="$(ns_generate_token | tr -d '\r\n')"
  if grep -qE '^GATEWAY_BEARER_TOKEN=' "$ENV_FILE"; then
    if [[ "$platform" == "macos" ]]; then
      sed -i '' "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$new_token/" "$ENV_FILE"
    else
      sed -i "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$new_token/" "$ENV_FILE"
    fi
  else
    printf '\nGATEWAY_BEARER_TOKEN=%s\n' "$new_token" >>"$ENV_FILE"
  fi
  ns_print_ok "Generated GATEWAY_BEARER_TOKEN in ${ENV_FILE}"
fi

ns_apply_env_overlay_file "$ENV_FILE" "${ENV_FILE}.local"

chmod 600 "$ENV_FILE" 2>/dev/null || true
ns_print_ok "Topology env materialized"
