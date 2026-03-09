#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
CONTAINER_NAME=""
ENDPOINTS=""
SHOW_MEMBERS="true"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/check-etcd-health.sh [--env-file PATH] [--container NAME] [--endpoints URLS] [--no-members]

Checks etcd endpoint health and member status using etcdctl inside the container when available.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --container)
      CONTAINER_NAME="${2:-}"
      shift 2
      ;;
    --endpoints)
      ENDPOINTS="${2:-}"
      shift 2
      ;;
    --no-members)
      SHOW_MEMBERS="false"
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

ns_require_cmd docker "docker" || exit 1

if [[ -z "$CONTAINER_NAME" && -f "$ENV_FILE" ]]; then
  CONTAINER_NAME="$(ns_env_get "$ENV_FILE" ETCD_CONTAINER_NAME nexus-etcd)"
fi
CONTAINER_NAME="${CONTAINER_NAME:-nexus-etcd}"

ENDPOINTS="${ENDPOINTS:-http://127.0.0.1:2379}"

ns_print_header "Etcd health"
echo "Container: ${CONTAINER_NAME}"
echo "Endpoints: ${ENDPOINTS}"

docker inspect "$CONTAINER_NAME" >/dev/null 2>&1 || ns_die "Container not found: $CONTAINER_NAME"

docker exec "$CONTAINER_NAME" env ETCDCTL_API=3 /usr/local/bin/etcdctl --endpoints="$ENDPOINTS" endpoint health
docker exec "$CONTAINER_NAME" env ETCDCTL_API=3 /usr/local/bin/etcdctl --endpoints="$ENDPOINTS" endpoint status --write-out=table

if [[ "$SHOW_MEMBERS" == "true" ]]; then
  docker exec "$CONTAINER_NAME" env ETCDCTL_API=3 /usr/local/bin/etcdctl --endpoints="$ENDPOINTS" member list --write-out=table
fi