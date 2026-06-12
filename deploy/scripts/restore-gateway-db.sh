#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
SNAPSHOT_PATH=""
DB_PATH=""
FORCE="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restore-gateway-db.sh --snapshot PATH [--env-file PATH] [--db-path PATH] [--force]

Restore a compressed gateway users.sqlite backup into the canonical runtime path.
The current DB is moved aside with a timestamp before the restored file is put in place.
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
    --db-path)
      DB_PATH="${2:-}"
      shift 2
      ;;
    --force)
      FORCE="true"
      shift
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

[[ -n "$SNAPSHOT_PATH" ]] || ns_die "--snapshot is required"
[[ -f "$SNAPSHOT_PATH" ]] || ns_die "Snapshot not found: $SNAPSHOT_PATH"

ns_require_cmd gzip "gzip" || exit 1
ns_require_cmd sqlite3 "sqlite3" || exit 1

runtime_root="$(ns_runtime_root "$ROOT_DIR")"
if [[ -z "$DB_PATH" ]]; then
  DB_PATH="${runtime_root}/gateway/data/users.sqlite"
fi

db_dir="$(dirname "$DB_PATH")"
timestamp="$(date +%Y%m%d-%H%M%S)"
backup_path="${DB_PATH}.pre-restore-${timestamp}"

if [[ "$FORCE" != "true" ]]; then
  echo "This will replace ${DB_PATH} with ${SNAPSHOT_PATH}."
  echo "The current DB will be moved to ${backup_path}."
  read -r -p "Continue? [y/N] " answer
  case "$answer" in
    [yY] | [yY][eE][sS]) ;;
    *)
      echo "Aborted."
      exit 1
      ;;
  esac
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
tmp_db="${tmp_dir}/users.sqlite"

ns_print_header "Restoring gateway database"
echo "Snapshot: ${SNAPSHOT_PATH}"
echo "Destination: ${DB_PATH}"

gzip -dc "$SNAPSHOT_PATH" >"$tmp_db"
integrity_result="$(sqlite3 "$tmp_db" 'PRAGMA integrity_check;' | tr -d '\r')"
if ! printf '%s\n' "$integrity_result" | grep -qx 'ok'; then
  ns_die "Snapshot integrity check failed: ${integrity_result}"
fi

ns_mkdir_p "$db_dir"
if [[ -f "$DB_PATH" ]]; then
  mv "$DB_PATH" "$backup_path"
fi
install -m 600 "$tmp_db" "$DB_PATH"

ns_print_ok "Gateway database restored"
echo "Previous DB: ${backup_path}"
