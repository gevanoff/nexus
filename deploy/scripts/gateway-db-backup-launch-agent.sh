#!/usr/bin/env bash
set -euo pipefail
umask 077

ENV_FILE="${NEXUS_GATEWAY_DB_BACKUP_ENV_FILE:-}"
[[ -n "$ENV_FILE" ]] || {
  echo "NEXUS_GATEWAY_DB_BACKUP_ENV_FILE is required" >&2
  exit 1
}
[[ -f "$ENV_FILE" ]] || {
  echo "Gateway DB backup env file not found: $ENV_FILE" >&2
  exit 1
}

set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

: "${NEXUS_REPO_DIR:?NEXUS_REPO_DIR is required}"

SCRIPT_PATH="${NEXUS_REPO_DIR}/deploy/scripts/backup-gateway-db.sh"
[[ -x "$SCRIPT_PATH" ]] || {
  echo "Backup script not found or not executable: $SCRIPT_PATH" >&2
  exit 1
}

cd "$NEXUS_REPO_DIR"
exec "$SCRIPT_PATH"