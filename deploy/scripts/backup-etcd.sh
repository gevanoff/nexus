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
KEEP_COUNT="${KEEP_COUNT:-30}"
SSH_TARGET="${SSH_TARGET:-}"
SSH_DIR="${SSH_DIR:-}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/backup-etcd.sh [--env-file PATH] [--container NAME] [--endpoints URLS] [--output PATH] [--keep COUNT] [--ssh-target USER@HOST --ssh-dir PATH] [--rclone-remote DEST]

Creates an etcd snapshot backup by running etcdctl inside the etcd container, copying the snapshot to the host, and optionally mirroring it off-host.
EOF
}

sha256_file() {
  local file_path="$1"
  if ns_have_cmd sha256sum; then
    sha256sum "$file_path" | awk '{print $1}'
    return 0
  fi
  if ns_have_cmd shasum; then
    shasum -a 256 "$file_path" | awk '{print $1}'
    return 0
  fi
  ns_die "sha256 tool not found (need sha256sum or shasum)"
}

require_integer() {
  local raw_value="$1"
  local name="$2"
  [[ "$raw_value" =~ ^[0-9]+$ ]] || ns_die "${name} must be a non-negative integer"
}

mirror_to_ssh() {
  local output_path="$1"
  local sha_path="$2"
  local manifest_path="$3"

  [[ -n "$SSH_TARGET" ]] || return 0
  [[ -n "$SSH_DIR" ]] || ns_die "--ssh-dir is required when --ssh-target is set"

  ns_require_cmd ssh "ssh" || exit 1
  local quoted_dir
  quoted_dir="$(printf '%q' "$SSH_DIR")"
  # shellcheck disable=SC2029
  ssh "$SSH_TARGET" "mkdir -p ${quoted_dir}"

  if ns_have_cmd rsync; then
    rsync -a "$output_path" "$sha_path" "$manifest_path" "$SSH_TARGET:$SSH_DIR/"
  else
    ns_require_cmd scp "scp" || exit 1
    scp "$output_path" "$sha_path" "$manifest_path" "$SSH_TARGET:$SSH_DIR/"
  fi

  ns_print_ok "Mirrored backup bundle to ${SSH_TARGET}:${SSH_DIR}"
}

mirror_to_rclone() {
  local output_path="$1"
  local sha_path="$2"
  local manifest_path="$3"

  [[ -n "$RCLONE_REMOTE" ]] || return 0
  ns_require_cmd rclone "rclone" || exit 1

  local remote_root="${RCLONE_REMOTE%/}"
  rclone copyto "$output_path" "${remote_root}/$(basename "$output_path")"
  rclone copyto "$sha_path" "${remote_root}/$(basename "$sha_path")"
  rclone copyto "$manifest_path" "${remote_root}/$(basename "$manifest_path")"

  ns_print_ok "Mirrored backup bundle to ${RCLONE_REMOTE}"
}

prune_local_backups() {
  local backup_dir="$1"
  local keep_count="$2"

  ((keep_count > 0)) || return 0
  [[ -d "$backup_dir" ]] || return 0

  local -a backup_files=()
  local file_path=""
  while IFS= read -r file_path; do
    backup_files+=("$file_path")
  done < <(find "$backup_dir" -maxdepth 1 -type f -name 'etcd-snapshot-*.db' -print | sort)

  local file_count="${#backup_files[@]}"
  ((file_count > keep_count)) || return 0

  local prune_count=$((file_count - keep_count))
  local manifest_path=""
  local sha_path=""
  for ((idx = 0; idx < prune_count; idx += 1)); do
    file_path="${backup_files[$idx]}"
    sha_path="${file_path}.sha256"
    manifest_path="${file_path%.db}.json"
    rm -f "$file_path" "$sha_path" "$manifest_path"
  done
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
    --keep)
      KEEP_COUNT="${2:-}"
      shift 2
      ;;
    --ssh-target)
      SSH_TARGET="${2:-}"
      shift 2
      ;;
    --ssh-dir)
      SSH_DIR="${2:-}"
      shift 2
      ;;
    --rclone-remote)
      RCLONE_REMOTE="${2:-}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

require_integer "$KEEP_COUNT" "--keep"

ns_require_cmd docker "docker" || exit 1
[[ -f "$ENV_FILE" ]] || ns_die "Env file not found: $ENV_FILE"

if [[ -z "$CONTAINER_NAME" && -f "$ENV_FILE" ]]; then
  CONTAINER_NAME="$(ns_env_get "$ENV_FILE" ETCD_CONTAINER_NAME nexus-etcd)"
fi
CONTAINER_NAME="${CONTAINER_NAME:-nexus-etcd}"

ENDPOINTS="${ENDPOINTS:-http://127.0.0.1:2379}"

if [[ -z "$OUTPUT_PATH" ]]; then
  timestamp="$(date +%Y%m%d-%H%M%S)"
  OUTPUT_PATH="$(ns_runtime_root_from_env "$ROOT_DIR" "$ENV_FILE")/etcd/backups/etcd-snapshot-${timestamp}.db"
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"
MANIFEST_PATH="${OUTPUT_PATH%.db}.json"
SHA_PATH="${OUTPUT_PATH}.sha256"

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

compressed_sha="$(sha256_file "$OUTPUT_PATH")"
snapshot_size="$(wc -c <"$OUTPUT_PATH" | tr -d '[:space:]')"
printf '%s  %s\n' "$compressed_sha" "$(basename "$OUTPUT_PATH")" >"$SHA_PATH"
cat >"$MANIFEST_PATH" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)",
  "container": "${CONTAINER_NAME}",
  "endpoints": "${ENDPOINTS}",
  "snapshot_file": "$(basename "$OUTPUT_PATH")",
  "snapshot_sha256": "${compressed_sha}",
  "snapshot_size_bytes": ${snapshot_size}
}
EOF

mirror_to_ssh "$OUTPUT_PATH" "$SHA_PATH" "$MANIFEST_PATH"
mirror_to_rclone "$OUTPUT_PATH" "$SHA_PATH" "$MANIFEST_PATH"
prune_local_backups "$(dirname "$OUTPUT_PATH")" "$KEEP_COUNT"

ns_print_ok "Snapshot saved to $OUTPUT_PATH"
