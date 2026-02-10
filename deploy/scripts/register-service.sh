#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <service-name> <base-url> <etcd-url>" >&2
  echo "  example: $0 ollama http://ollama:11434 http://etcd:2379" >&2
  exit 1
fi

name="$1"
base_url="$2"
etcd_url="$3"

metadata_url="${base_url%/}/v1/metadata"

payload=$(python3 - <<PY
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

echo "Registered $name at $base_url"
