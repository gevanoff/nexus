#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <etcd-url>" >&2
  echo "  example: $0 http://etcd:2379" >&2
  exit 1
fi

etcd_url="$1"

payload=$(python3 - <<'PY'
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
