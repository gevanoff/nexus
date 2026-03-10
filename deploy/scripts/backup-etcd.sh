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
OUTPUT_PATH=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/backup-etcd.sh [--env-file PATH] [--container NAME] [--endpoints URLS] [--output PATH]

Creates an etcd snapshot backup by running etcdctl inside the etcd container and copying the snapshot to the host.
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
    --output)
      OUTPUT_PATH="${2:-}"
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

ns_require_cmd docker "docker" || exit 1

if [[ -z "$CONTAINER_NAME" && -f "$ENV_FILE" ]]; then
  CONTAINER_NAME="$(ns_env_get "$ENV_FILE" ETCD_CONTAINER_NAME nexus-etcd)"
fi
CONTAINER_NAME="${CONTAINER_NAME:-nexus-etcd}"

ENDPOINTS="${ENDPOINTS:-http://127.0.0.1:2379}"

if [[ -z "$OUTPUT_PATH" ]]; then
  timestamp="$(date +%Y%m%d-%H%M%S)"
  OUTPUT_PATH="$ROOT_DIR/.runtime/etcd/backups/etcd-snapshot-${timestamp}.db"
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"

tmp_snapshot="/tmp/nexus-etcd-snapshot.db"
docker inspect "$CONTAINER_NAME" >/dev/null 2>&1 || ns_die "Container not found: $CONTAINER_NAME"

ns_print_header "Backing up etcd"
echo "Container: ${CONTAINER_NAME}"
echo "Endpoints: ${ENDPOINTS}"
echo "Output: ${OUTPUT_PATH}"

docker exec "$CONTAINER_NAME" rm -f "$tmp_snapshot" >/dev/null 2>&1 || true
docker exec -e ETCDCTL_API=3 "$CONTAINER_NAME" /usr/local/bin/etcdctl --endpoints="$ENDPOINTS" snapshot save "$tmp_snapshot"
docker cp "${CONTAINER_NAME}:${tmp_snapshot}" "$OUTPUT_PATH"
docker exec "$CONTAINER_NAME" rm -f "$tmp_snapshot" >/dev/null 2>&1 || true

ns_print_ok "Snapshot saved to $OUTPUT_PATH"