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

usage() {
  cat <<'EOF'
Usage: deploy/scripts/list-services.sh [--yes] [--json] <etcd-url>

Example:
  deploy/scripts/list-services.sh http://etcd:2379

Options:
  --yes    Non-interactive mode (assume "yes" for install prompts)
  --json   Print decoded service records as JSON
EOF
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

response="$(curl -fsS -X POST "${etcd_url%/}/v3/kv/range" \
  -H "Content-Type: application/json" \
  -d "$payload")"

$PYTHON - "$NS_OUTPUT_FORMAT" <<'PY' <<<"$response"
import base64
import json
import sys


def decode(raw: str) -> str:
  return base64.b64decode(raw.encode("ascii")).decode("utf-8")


output_format = sys.argv[1]
raw = sys.stdin.read()
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
    }
  )

records.sort(key=lambda item: item["name"])

if output_format == "json":
  print(json.dumps(records, indent=2, sort_keys=True))
  raise SystemExit(0)

if not records:
  print("No service registrations found.")
  raise SystemExit(0)

headers = ["NAME", "BASE URL", "BACKEND CLASS", "METADATA URL"]
rows = [
  [
    record["name"],
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
