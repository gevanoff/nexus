#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
SNAPSHOT_PATH=""
CONTAINER_NAME=""
DATA_DIR="$ROOT_DIR/.runtime/etcd/data"
FORCE="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restore-etcd.sh --snapshot PATH [--env-file PATH] [--container NAME] [--data-dir PATH] [--force]

Restores an etcd snapshot into the host data directory used by docker-compose.etcd.yml.
This stops the container, moves the existing data directory aside, restores the snapshot, and restarts etcd.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --snapshot)
      SNAPSHOT_PATH="${2:-}"
      shift 2
      ;;
    --container)
      CONTAINER_NAME="${2:-}"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="${2:-}"
      shift 2
      ;;
    --force)
      FORCE="true"
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

[[ -n "$SNAPSHOT_PATH" ]] || ns_die "--snapshot is required"
[[ -f "$SNAPSHOT_PATH" ]] || ns_die "Snapshot not found: $SNAPSHOT_PATH"

ns_require_cmd docker "docker" || exit 1
if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi

if [[ -z "$CONTAINER_NAME" && -f "$ENV_FILE" ]]; then
  CONTAINER_NAME="$(ns_env_get "$ENV_FILE" ETCD_CONTAINER_NAME nexus-etcd)"
fi
CONTAINER_NAME="${CONTAINER_NAME:-nexus-etcd}"

member_name="$(ns_env_get "$ENV_FILE" ETCD_NAME nexus-etcd)"
peer_url="$(ns_env_get "$ENV_FILE" ETCD_INITIAL_ADVERTISE_PEER_URLS http://etcd:2380)"
initial_cluster="$(ns_env_get "$ENV_FILE" ETCD_INITIAL_CLUSTER nexus-etcd=http://etcd:2380)"
cluster_token="$(ns_env_get "$ENV_FILE" ETCD_INITIAL_CLUSTER_TOKEN nexus-etcd-cluster)"

if [[ "$FORCE" != "true" ]]; then
  echo "This will stop ${CONTAINER_NAME}, move the current etcd data aside, restore ${SNAPSHOT_PATH}, and restart etcd."
  read -r -p "Continue? [y/N] " answer
  case "$answer" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_dir="${DATA_DIR}.pre-restore-${timestamp}"

ns_print_header "Restoring etcd"
echo "Container: ${CONTAINER_NAME}"
echo "Snapshot: ${SNAPSHOT_PATH}"
echo "Data dir: ${DATA_DIR}"

ns_compose --env-file "$ENV_FILE" -f docker-compose.etcd.yml down

if [[ -d "$DATA_DIR" ]]; then
  mv "$DATA_DIR" "$backup_dir"
fi
mkdir -p "$DATA_DIR"

docker run --rm \
  -v "$SNAPSHOT_PATH:/snapshot.db:ro" \
  -v "$DATA_DIR:/restore-data" \
  quay.io/coreos/etcd:v3.5.11 \
  /usr/local/bin/etcdctl snapshot restore /snapshot.db \
    --name "$member_name" \
    --data-dir /restore-data \
    --initial-cluster "$initial_cluster" \
    --initial-advertise-peer-urls "$peer_url" \
    --initial-cluster-token "$cluster_token"

ns_compose --env-file "$ENV_FILE" -f docker-compose.etcd.yml up -d

ns_print_ok "Restore complete"
echo "Previous data moved to: ${backup_dir}"
echo "Verify health with: ./deploy/scripts/check-etcd-health.sh --env-file ${ENV_FILE}"