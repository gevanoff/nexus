#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
MEMBER_NAME=""
CLIENT_URL=""
PEER_URL=""
INITIAL_CLUSTER=""
CLUSTER_STATE="new"
CLUSTER_TOKEN="nexus-etcd-cluster"
PRINT_ONLY="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/init-etcd-cluster.sh --name NAME --client-url URL --peer-url URL --initial-cluster MEMBERS [--env-file PATH] [--cluster-state new|existing] [--cluster-token TOKEN] [--print-only]

Example:
  ./deploy/scripts/init-etcd-cluster.sh \
    --name ai1-etcd \
    --client-url http://ai1:2379 \
    --peer-url http://ai1:2380 \
    --initial-cluster ai1-etcd=http://ai1:2380,ada2-etcd=http://ada2:2380

This updates the target env file with the etcd member settings needed by docker-compose.etcd.yml.
Run it once on each etcd host with that host's own name/client/peer URLs.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --name)
      MEMBER_NAME="${2:-}"
      shift 2
      ;;
    --client-url)
      CLIENT_URL="${2:-}"
      shift 2
      ;;
    --peer-url)
      PEER_URL="${2:-}"
      shift 2
      ;;
    --initial-cluster)
      INITIAL_CLUSTER="${2:-}"
      shift 2
      ;;
    --cluster-state)
      CLUSTER_STATE="${2:-}"
      shift 2
      ;;
    --cluster-token)
      CLUSTER_TOKEN="${2:-}"
      shift 2
      ;;
    --print-only)
      PRINT_ONLY="true"
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

[[ -n "$MEMBER_NAME" ]] || ns_die "--name is required"
[[ -n "$CLIENT_URL" ]] || ns_die "--client-url is required"
[[ -n "$PEER_URL" ]] || ns_die "--peer-url is required"
[[ -n "$INITIAL_CLUSTER" ]] || ns_die "--initial-cluster is required"

case "$CLUSTER_STATE" in
  new|existing) ;;
  *) ns_die "--cluster-state must be 'new' or 'existing'" ;;
esac

client_port="${CLIENT_URL##*:}"
peer_port="${PEER_URL##*:}"

updates=$(cat <<EOF
ETCD_CONTAINER_NAME=nexus-etcd
ETCD_NAME=${MEMBER_NAME}
ETCD_PORT=${client_port}
ETCD_PEER_PORT=${peer_port}
ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:${client_port}
ETCD_ADVERTISE_CLIENT_URLS=${CLIENT_URL}
ETCD_LISTEN_PEER_URLS=http://0.0.0.0:${peer_port}
ETCD_INITIAL_ADVERTISE_PEER_URLS=${PEER_URL}
ETCD_INITIAL_CLUSTER=${INITIAL_CLUSTER}
ETCD_INITIAL_CLUSTER_STATE=${CLUSTER_STATE}
ETCD_INITIAL_CLUSTER_TOKEN=${CLUSTER_TOKEN}
EOF
)

if [[ "$PRINT_ONLY" == "true" ]]; then
  printf '%s\n' "$updates"
  exit 0
fi

ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"

tmp_file="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "$tmp_file"' EXIT

cp "$ENV_FILE" "$tmp_file"

while IFS='=' read -r key value; do
  [[ -n "$key" ]] || continue
  if grep -q -E "^[[:space:]]*${key}=" "$tmp_file"; then
    if sed --version >/dev/null 2>&1; then
      sed -i -E "s#^[[:space:]]*${key}=.*#${key}=${value//#/\\#}#" "$tmp_file"
    else
      sed -i '' -E "s#^[[:space:]]*${key}=.*#${key}=${value//#/\\#}#" "$tmp_file"
    fi
  else
    printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
  fi
done <<< "$updates"

mv "$tmp_file" "$ENV_FILE"
chmod 600 "$ENV_FILE"
trap - EXIT

ns_print_ok "Updated $ENV_FILE for etcd member ${MEMBER_NAME}"
echo
echo "Next steps:"
echo "  1) Review ETCD_* values in ${ENV_FILE}"
echo "  2) Start or restart etcd: docker compose -f docker-compose.etcd.yml up -d"
echo "  3) Verify health: ./deploy/scripts/check-etcd-health.sh --env-file ${ENV_FILE}"