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
Usage: deploy/scripts/register-service.sh [--yes] [--backend-class CLASS] [--hostname HOSTNAME] <service-name> <base-url> <etcd-url>

Example:
  deploy/scripts/register-service.sh --backend-class local_vllm --hostname ai1 vllm http://ai1:8000/v1 http://etcd:2379

Options:
  --backend-class CLASS  Canonical backend class (for example: local_vllm, gpu_heavy)
  --hostname HOSTNAME    Hostname where the service is running (defaults to the base URL host)
  --yes                  Non-interactive mode (assume "yes" for install prompts)
EOF
}

parse_args() {
  backend_class=""
  hostname=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --backend-class)
        backend_class="${2:-}"
        shift 2
        ;;
      --hostname)
        hostname="${2:-}"
        shift 2
        ;;
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

payload=$($PYTHON - "$name" "$base_url" "$metadata_url" "$backend_class" "$hostname" <<'PY'
import base64, json, sys
try:
  from urllib.parse import urlparse
except ImportError:
  from urlparse import urlparse

name, base_url, metadata_url, backend_class, hostname = sys.argv[1:6]
key = "/nexus/services/{}".format(name)
if not hostname:
  try:
    hostname = (urlparse(base_url).hostname or "").strip()
  except Exception:
    hostname = ""
payload = {"name": name, "base_url": base_url, "metadata_url": metadata_url}
if backend_class:
  payload["backend_class"] = backend_class
if hostname:
  payload["hostname"] = hostname
value = json.dumps(payload)
print(json.dumps({
  "key": base64.b64encode(key.encode()).decode(),
  "value": base64.b64encode(value.encode()).decode()
}))
PY
)

curl -fsS -X POST "${etcd_url%/}/v3/kv/put" \
  -H "Content-Type: application/json" \
  -d "$payload"

extra_info=""
if [[ -n "$backend_class" ]]; then
  extra_info="backend_class=$backend_class"
fi
if [[ -n "$hostname" ]]; then
  if [[ -n "$extra_info" ]]; then
    extra_info="$extra_info, "
  fi
  extra_info="${extra_info}hostname=$hostname"
fi
if [[ -n "$extra_info" ]]; then
  ns_print_ok "Registered $name at $base_url ($extra_info)"
else
  ns_print_ok "Registered $name at $base_url"
fi
