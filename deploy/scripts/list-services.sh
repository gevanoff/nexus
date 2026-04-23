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
NS_OUTPUT_FORMAT="table"
DEFAULT_ETCD_URL=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/list-services.sh [--yes] [--json] [etcd-url]

Example:
  deploy/scripts/list-services.sh
  deploy/scripts/list-services.sh http://ai1:2379

Options:
  --yes    Non-interactive mode (assume "yes" for install prompts)
  --json   Print decoded service records as JSON
EOF
}

resolve_default_etcd_url() {
  local env_file
  env_file="$(ns_guess_env_file "$ROOT_DIR")"

  local etcd_port
  etcd_port="$(ns_env_get "$env_file" ETCD_PORT "2379")"
  if ! ns_is_valid_port "$etcd_port"; then
    etcd_port="2379"
  fi

  local configured_url
  configured_url="$(ns_env_get "$env_file" ETCD_URL "http://localhost:${etcd_port}")"
  if [[ -z "$configured_url" ]]; then
    echo "http://localhost:${etcd_port}"
    return 0
  fi

  local host_part
  host_part="$($PYTHON - "$configured_url" <<'PY'
from urllib.parse import urlparse
import sys
parsed = urlparse(sys.argv[1])
print((parsed.hostname or "").strip().lower())
PY
)"

  case "$host_part" in
    ""|0.0.0.0|127.0.0.1|localhost|etcd)
      echo "http://localhost:${etcd_port}"
      ;;
    *)
      echo "$configured_url"
      ;;
  esac
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
        ;;
      --json)
        NS_OUTPUT_FORMAT="json"
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

  if [[ $# -gt 1 ]]; then
    usage >&2
    exit 1
  fi

  if [[ $# -eq 1 ]]; then
    etcd_url="$1"
  else
    etcd_url="$DEFAULT_ETCD_URL"
  fi
}

ns_print_header "Ensuring prerequisites"
ns_ensure_prereqs false true false false true false || true

ns_require_cmd curl "curl" || exit 1

PYTHON="$(ns_pick_python)"
if [[ -z "$PYTHON" ]]; then
  ns_print_error "python3/python is required but not installed."
  exit 1
fi

DEFAULT_ETCD_URL="$(resolve_default_etcd_url)"

parse_args "$@"

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

response="$(curl -fsS -X POST "${etcd_url%/}/v3/kv/range" \
  -H "Content-Type: application/json" \
  -d "$payload")"

RESPONSE_JSON="$response" $PYTHON - "$NS_OUTPUT_FORMAT" <<'PY'
import base64
import json
import os
import sys


def decode(raw: str) -> str:
  return base64.b64decode(raw.encode("ascii")).decode("utf-8")


output_format = sys.argv[1]
raw = os.environ.get("RESPONSE_JSON", "")
data = json.loads(raw) if raw.strip() else {}

records = []
for item in data.get("kvs", []) if isinstance(data, dict) else []:
  try:
    key = decode(str(item.get("key", "")))
    value = json.loads(decode(str(item.get("value", ""))))
  except Exception:
    continue
  if not isinstance(value, dict):
    continue
  records.append(
    {
      "key": key,
      "name": str(value.get("name") or key.rsplit("/", 1)[-1]),
      "base_url": str(value.get("base_url") or ""),
      "metadata_url": str(value.get("metadata_url") or ""),
      "backend_class": str(value.get("backend_class") or ""),
      "hostname": str(value.get("hostname") or ""),
    }
  )

records.sort(key=lambda item: item["name"])

if output_format == "json":
  print(json.dumps(records, indent=2, sort_keys=True))
  raise SystemExit(0)

if not records:
  print("No service registrations found.")
  raise SystemExit(0)

headers = ["NAME", "HOSTNAME", "BASE URL", "BACKEND CLASS", "METADATA URL"]
rows = [
  [
    record["name"],
    record["hostname"] or "-",
    record["base_url"] or "-",
    record["backend_class"] or "-",
    record["metadata_url"] or "-",
  ]
  for record in records
]
widths = [len(header) for header in headers]
for row in rows:
  for index, cell in enumerate(row):
    widths[index] = max(widths[index], len(cell))

def format_row(values):
  return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values))

print(format_row(headers))
print(format_row(["-" * width for width in widths]))
for row in rows:
  print(format_row(row))
PY
