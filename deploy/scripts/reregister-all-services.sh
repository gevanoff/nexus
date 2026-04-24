#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETCD_URL="${ETCD_URL:-http://localhost:2379}"

PYTHON="$(ns_pick_python)"
if [[ -z "$PYTHON" ]]; then
  ns_print_error "python3/python is required but not installed."
  exit 1
fi

echo "Re-registering all discovered services with canonical metadata..."

services_json="$("$SCRIPT_DIR/list-services.sh" --yes --json "$ETCD_URL")"
mapfile -t registrations < <(
  SERVICES_JSON="$services_json" "$PYTHON" - <<'PY'
import json
import os

records = json.loads(os.environ.get("SERVICES_JSON", "[]") or "[]")
if not isinstance(records, list):
  raise SystemExit("service listing did not return a JSON array")

for record in records:
  if not isinstance(record, dict):
    continue
  name = str(record.get("name") or "").strip()
  base_url = str(record.get("base_url") or "").strip()
  backend_class = str(record.get("backend_class") or "").strip()
  hostname = str(record.get("hostname") or "").strip()
  if not name or not base_url:
    continue
  print("\t".join([name, base_url, backend_class, hostname]))
PY
)

if [[ "${#registrations[@]}" -eq 0 ]]; then
  echo "No services are currently registered in etcd."
  exit 0
fi

for row in "${registrations[@]}"; do
  IFS=$'\t' read -r name base_url backend_class hostname <<<"$row"
  args=(--yes)
  if [[ -n "$backend_class" ]]; then
    args+=(--backend-class "$backend_class")
  fi
  if [[ -n "$hostname" ]]; then
    args+=(--hostname "$hostname")
  fi
  "$SCRIPT_DIR/register-service.sh" "${args[@]}" "$name" "$base_url" "$ETCD_URL"
done

echo "Re-registered ${#registrations[@]} services."
