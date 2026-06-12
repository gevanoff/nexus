#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
DB_PATH="${DB_PATH:-}"
BACKUP_DIR="${BACKUP_DIR:-}"
OUTPUT_PATH="${OUTPUT_PATH:-}"
KEEP_COUNT="${KEEP_COUNT:-30}"
SSH_TARGET="${SSH_TARGET:-}"
SSH_DIR="${SSH_DIR:-}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/backup-gateway-db.sh [options]

Create a consistent snapshot of the gateway users database using SQLite's native
.backup command, then compress it on the host.

Options:
  --env-file PATH        Repo env file used to resolve the canonical runtime root
  --db-path PATH         Source SQLite database path
  --backup-dir PATH      Directory for timestamped local backups
  --output PATH          Explicit output path for the compressed backup
  --keep COUNT           Number of local compressed backups to retain (default: 30)
  --ssh-target USER@HOST Mirror the backup bundle to a remote host over SSH
  --ssh-dir PATH         Destination directory for --ssh-target
  --rclone-remote DEST   Mirror the backup bundle to a configured rclone destination
  -h, --help             Show this help text

Environment variable defaults:
  ENV_FILE, DB_PATH, BACKUP_DIR, OUTPUT_PATH, KEEP_COUNT,
  SSH_TARGET, SSH_DIR, RCLONE_REMOTE
EOF
}

resolve_runtime_root() {
  local repo_dir="$1"
  local env_file="$2"
  local configured_root=""

  configured_root="${NEXUS_RUNTIME_ROOT:-}"
  if [[ -z "$configured_root" ]]; then
    configured_root="$(ns_env_get "$env_file" NEXUS_RUNTIME_ROOT "")"
  fi
  if [[ -z "$configured_root" ]]; then
    printf '%s\n' "${repo_dir}/.runtime"
    return 0
  fi

  case "$configured_root" in
    /*)
      printf '%s\n' "$configured_root"
      ;;
    *)
      printf '%s\n' "${repo_dir}/${configured_root#./}"
      ;;
  esac
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
  local host_tag="$3"

  (( keep_count > 0 )) || return 0
  [[ -d "$backup_dir" ]] || return 0

  local -a backup_files=()
  local file_path=""
  while IFS= read -r file_path; do
    backup_files+=("$file_path")
  done < <(find "$backup_dir" -maxdepth 1 -type f -name "gateway-users-${host_tag}-*.sqlite3.gz" -print | sort)

  local file_count="${#backup_files[@]}"
  (( file_count > keep_count )) || return 0

  local prune_count=$((file_count - keep_count))
  for ((idx=0; idx<prune_count; idx+=1)); do
    file_path="${backup_files[$idx]}"
    rm -f "$file_path" "$file_path.sha256" "${file_path%.sqlite3.gz}.json"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --db-path)
      DB_PATH="${2:-}"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="${2:-}"
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
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

require_integer "$KEEP_COUNT" "--keep"
ns_require_cmd sqlite3 "sqlite3" || exit 1
ns_require_cmd gzip "gzip" || exit 1

runtime_root="$(resolve_runtime_root "$ROOT_DIR" "$ENV_FILE")"

if [[ -z "$DB_PATH" ]]; then
  DB_PATH="${runtime_root}/gateway/data/users.sqlite"
fi

[[ -f "$DB_PATH" ]] || ns_die "Gateway database not found: $DB_PATH"

if [[ -n "$OUTPUT_PATH" ]]; then
  BACKUP_DIR="$(dirname "$OUTPUT_PATH")"
else
  if [[ -z "$BACKUP_DIR" ]]; then
    BACKUP_DIR="${runtime_root}/gateway/backups"
  fi
  host_tag="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)"
  timestamp="$(date +%Y%m%d-%H%M%S)"
  OUTPUT_PATH="${BACKUP_DIR}/gateway-users-${host_tag}-${timestamp}.sqlite3.gz"
fi

MANIFEST_PATH="${OUTPUT_PATH%.sqlite3.gz}.json"
SHA_PATH="${OUTPUT_PATH}.sha256"
host_tag="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)"

ns_mkdir_p "$BACKUP_DIR"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
tmp_db="${tmp_dir}/users.sqlite"

ns_print_header "Backing up gateway database"
echo "Source DB: ${DB_PATH}"
echo "Output: ${OUTPUT_PATH}"
if [[ -n "$SSH_TARGET" ]]; then
  echo "SSH mirror: ${SSH_TARGET}:${SSH_DIR}"
fi
if [[ -n "$RCLONE_REMOTE" ]]; then
  echo "rclone mirror: ${RCLONE_REMOTE}"
fi

sqlite3 "$DB_PATH" <<SQL
.timeout 5000
.backup '$tmp_db'
SQL

integrity_result="$(sqlite3 "$tmp_db" 'PRAGMA integrity_check;' | tr -d '\r')"
if ! printf '%s\n' "$integrity_result" | grep -qx 'ok'; then
  ns_die "Backup integrity check failed: ${integrity_result}"
fi

gzip -n -c "$tmp_db" > "$OUTPUT_PATH"
compressed_sha="$(sha256_file "$OUTPUT_PATH")"
compressed_size="$(wc -c < "$OUTPUT_PATH" | tr -d '[:space:]')"
source_size="$(wc -c < "$tmp_db" | tr -d '[:space:]')"

printf '%s  %s\n' "$compressed_sha" "$(basename "$OUTPUT_PATH")" > "$SHA_PATH"
cat > "$MANIFEST_PATH" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "${host_tag}",
  "source_db": "${DB_PATH}",
  "backup_file": "$(basename "$OUTPUT_PATH")",
  "compressed_sha256": "${compressed_sha}",
  "source_size_bytes": ${source_size},
  "compressed_size_bytes": ${compressed_size}
}
EOF

mirror_to_ssh "$OUTPUT_PATH" "$SHA_PATH" "$MANIFEST_PATH"
mirror_to_rclone "$OUTPUT_PATH" "$SHA_PATH" "$MANIFEST_PATH"
prune_local_backups "$BACKUP_DIR" "$KEEP_COUNT" "$host_tag"

ns_print_ok "Gateway DB backup saved to $OUTPUT_PATH"