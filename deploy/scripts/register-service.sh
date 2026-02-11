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
Usage: deploy/scripts/register-service.sh [--yes] <service-name> <base-url> <etcd-url>

Example:
  deploy/scripts/register-service.sh ollama http://ollama:11434 http://etcd:2379

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

  if [[ $# -lt 3 ]]; then
    usage >&2
    exit 1
  fi

  name="$1"
  base_url="$2"
  etcd_url="$3"
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

metadata_url="${base_url%/}/v1/metadata"

payload=$($PYTHON - <<PY
import base64, json, sys
name, base_url, metadata_url = sys.argv[1:4]
key = f"/nexus/services/{name}"
value = json.dumps({"name": name, "base_url": base_url, "metadata_url": metadata_url})
print(json.dumps({
  "key": base64.b64encode(key.encode()).decode(),
  "value": base64.b64encode(value.encode()).decode()
}))
PY
"$name" "$base_url" "$metadata_url")

curl -fsS -X POST "${etcd_url%/}/v3/kv/put" \
  -H "Content-Type: application/json" \
  -d "$payload"

ns_print_ok "Registered $name at $base_url"
