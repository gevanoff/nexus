#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"
admin_flag="true"
db_path="$ROOT_DIR/.runtime/gateway/data/users.sqlite"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/set-user-admin.sh [--yes] [--revoke] [--db-path PATH] <username>

Examples:
  deploy/scripts/set-user-admin.sh alice
  deploy/scripts/set-user-admin.sh --revoke alice

Options:
  --yes            Non-interactive mode (assume "yes" for install prompts)
  --revoke         Remove admin privileges instead of granting them
  --db-path PATH   Override the host-side gateway users.sqlite path
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
        ;;
      --revoke)
        admin_flag="false"
        shift
        ;;
      --db-path)
        if [[ $# -lt 2 ]]; then
          ns_print_error "--db-path requires a value"
          usage >&2
          exit 2
        fi
        db_path="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -* )
        ns_print_error "Unknown option: $1"
        usage >&2
        exit 2
        ;;
      *)
        break
        ;;
    esac
  done

  if [[ $# -ne 1 ]]; then
    usage >&2
    exit 1
  fi

  username="$1"
}

parse_args "$@"

ns_print_header "Ensuring prerequisites"
ns_ensure_prereqs false true false false true false || true

PYTHON="$(ns_pick_python)"
if [[ -z "$PYTHON" ]]; then
  ns_print_error "python3/python is required but not installed."
  exit 1
fi

if [[ ! -f "$db_path" ]]; then
  ns_print_error "Gateway user database not found at: $db_path"
  ns_print_warn "Start the gateway at least once or pass --db-path to the correct host-side users.sqlite file."
  exit 1
fi

"$PYTHON" - "$ROOT_DIR" "$db_path" "$username" "$admin_flag" <<'PY'
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
db_path = sys.argv[2]
username = sys.argv[3]
admin = sys.argv[4].strip().lower() == "true"

sys.path.insert(0, str(root_dir / "services" / "gateway"))

from app import user_store  # noqa: E402

user_store.init_db(db_path)
user_store.set_admin(db_path, username=username, admin=admin)
print(f"{'Granted' if admin else 'Revoked'} admin for {username}")
PY

ns_print_ok "Updated admin flag for $username"