#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

CONTAINER="${GATEWAY_CONTAINER_NAME:-nexus-gateway}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/verify-gateway.sh [--container NAME]

Run the comprehensive Gateway verifier inside the running Gateway container.
The verifier reads its bearer token from the container environment and always
uses the container's internal ports (8800 and 8801), so host port remapping and
upstream placement do not affect verification.

Options:
  --container NAME  Gateway container name (default: nexus-gateway)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
        ns_die "--container requires a value"
      fi
      CONTAINER="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      ns_print_error "Unknown argument: $1"
      usage >&2
      exit 2
      ;;
  esac
done

ns_require_cmd docker

container_status="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$container_status" != "running" ]]; then
  ns_die "Gateway container is not running: ${CONTAINER} (status: ${container_status:-missing})"
fi

ns_print_header "Gateway Verifier"
echo "Container: ${CONTAINER}"

docker exec -i "$CONTAINER" \
  python3 /var/lib/gateway/tools/verify_gateway.py \
  --skip-pytest \
  --base-url http://127.0.0.1:8800 \
  --obs-url http://127.0.0.1:8801

ns_print_ok "Verifier passed"
