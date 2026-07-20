#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd curl
PYTHON="$(ns_pick_python || true)"
[[ -n "${PYTHON:-}" ]] || ns_die "python3/python is required."

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
BASE_URL="${ACE_STEP_BASE_URL:-}"
PROMPT="${MUSIC_SMOKE_PROMPT:-Instrumental cinematic ambient music with warm analog synthesizers and a restrained pulse}"
DURATION="${MUSIC_SMOKE_DURATION:-20}"
TIMEOUT_SEC="${MUSIC_SMOKE_TIMEOUT_SEC:-1800}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --prompt) PROMPT="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-}"; shift 2 ;;
    --timeout-sec) TIMEOUT_SEC="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: deploy/scripts/smoke-test-music.sh [--env-file PATH] [--base-url URL] [--prompt TEXT] [--duration N] [--timeout-sec N]"
      exit 0
      ;;
    *) ns_die "Unknown argument: $1" ;;
  esac
done

if [[ -z "$BASE_URL" ]]; then
  BASE_URL="$(ns_env_get "$ENV_FILE" ACE_STEP_BASE_URL "http://127.0.0.1:9195")"
fi

payload="$("$PYTHON" - "$PROMPT" "$DURATION" <<'PY'
import json, sys
print(json.dumps({
    "prompt": sys.argv[1],
    "audio_duration": int(sys.argv[2]),
    "thinking": True,
    "use_random_seed": False,
    "seed": 42,
}))
PY
)"

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT

ns_print_header "ACE-Step Music Smoke Test"
echo "URL: $BASE_URL"

curl -fsS --max-time 10 "${BASE_URL%/}/healthz" >/dev/null
curl -fsS --max-time 30 "${BASE_URL%/}/readyz" >/dev/null
meta="$(curl -sS --max-time "$TIMEOUT_SEC" -o "$body_file" -w '%{http_code} %{time_total}' \
  -H 'Content-Type: application/json' \
  -X POST "${BASE_URL%/}/v1/music/generations" \
  -d "$payload" || true)"
status="${meta%% *}"
elapsed="${meta##* }"

if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
  ns_print_error "Music generation failed with HTTP $status after ${elapsed}s"
  head -c 4000 "$body_file"; echo
  exit 1
fi

"$PYTHON" - "$body_file" "$elapsed" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
url = payload.get("audio_url") or payload.get("url")
if not url:
    raise SystemExit(f"generation succeeded without audio URL: {payload}")
print(f"elapsed={sys.argv[2]}s")
print(f"url={url}")
PY
