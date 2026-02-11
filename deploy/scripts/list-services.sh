#!/usr/bin/env bash
set -euo pipefail
umask 077

# Maintainer note:
# Reuse shared helpers from deploy/scripts/_common.sh for prereqs, python selection,
# and input handling. Add helpers there rather than duplicating logic.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/list-services.sh [--yes] <etcd-url>

Example:
  deploy/scripts/list-services.sh http://etcd:2379

Options:
  --yes   Non-interactive mode (assume "yes" for install prompts)
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        ns_print_error "Unknown option: $1"
        usage
        exit 2
        ;;
      *)
        break
        ;;
    esac
  done

  if [[ $# -lt 1 ]]; then
    usage >&2
    exit 1
  fi

  etcd_url="$1"
}

parse_args "$@"

ns_print_header "Ensuring prerequisites"
ns_ensure_prereqs false true false false true false || true

ns_require_cmd curl "curl" || exit 1

PYTHON="$(ns_pick_python)"
if [[ -z "$PYTHON" ]]; then
  ns_print_error "python3/python is required but not installed."
  exit 1
fi

payload=$($PYTHON - <<'PY'
import base64, json
prefix = "/nexus/services/"
range_end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
print(json.dumps({
  "key": base64.b64encode(prefix.encode()).decode(),
  "range_end": base64.b64encode(range_end.encode()).decode()
}))
PY
)

curl -fsS -X POST "${etcd_url%/}/v3/kv/range" \
  -H "Content-Type: application/json" \
  -d "$payload"
