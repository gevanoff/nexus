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
BACKEND_CLASS="${VIDEO_SMOKE_BACKEND_CLASS:-ltx_video}"
BACKEND_URL="${VIDEO_SMOKE_BACKEND_URL:-}"
PROMPT="${VIDEO_SMOKE_PROMPT:-A calm foggy mountain valley at sunrise, cinematic, slow camera motion}"
DURATION="${VIDEO_SMOKE_DURATION:-3}"
RESOLUTION="${VIDEO_SMOKE_RESOLUTION:-720p}"
FPS="${VIDEO_SMOKE_FPS:-24}"
TIMEOUT_SEC="${VIDEO_SMOKE_TIMEOUT_SEC:-3600}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/smoke-test-video.sh [options]

Run a direct smoke test against an LTX-2.3 or HunyuanVideo-1.5 Nexus shim.

Options:
  --env-file PATH         Env file path (default: ./.env)
  --backend-class CLASS   ltx_video or hunyuan_video (default: ltx_video)
  --backend-url URL       Override the backend base URL
  --prompt TEXT           Prompt to submit
  --duration N            Duration in seconds (default: 3)
  --resolution VALUE      Resolution label (default: 720p)
  --fps N                 Frame rate (default: 24)
  --timeout-sec N         curl max-time (default: 3600)
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --backend-class) BACKEND_CLASS="${2:-}"; shift 2 ;;
    --backend-url) BACKEND_URL="${2:-}"; shift 2 ;;
    --prompt) PROMPT="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-}"; shift 2 ;;
    --resolution) RESOLUTION="${2:-}"; shift 2 ;;
    --fps) FPS="${2:-}"; shift 2 ;;
    --timeout-sec) TIMEOUT_SEC="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) ns_die "Unknown argument: $1" ;;
  esac
done

case "$BACKEND_CLASS" in
  ltx_video)
    env_key="LTX_VIDEO_BASE_URL"
    default_url="http://127.0.0.1:9180"
    ;;
  hunyuan_video)
    env_key="HUNYUAN_VIDEO_BASE_URL"
    default_url="http://127.0.0.1:9185"
    ;;
  *) ns_die "Unsupported backend class: $BACKEND_CLASS" ;;
esac

if [[ -z "$BACKEND_URL" ]]; then
  BACKEND_URL="$(ns_env_get "$ENV_FILE" "$env_key" "$default_url")"
fi

payload="$($PYTHON - "$PROMPT" "$DURATION" "$RESOLUTION" "$FPS" <<'PY'
import json, sys
prompt, duration, resolution, fps = sys.argv[1:5]
print(json.dumps({
    "prompt": prompt,
    "duration_seconds": int(duration),
    "resolution": resolution,
    "fps": int(fps),
    "seed": 42,
}))
PY
)"

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT

ns_print_header "Video Smoke Test"
echo "Backend: $BACKEND_CLASS"
echo "URL: $BACKEND_URL"

curl -fsS --max-time 10 "${BACKEND_URL%/}/healthz" >/dev/null
curl -fsS --max-time 30 "${BACKEND_URL%/}/readyz" >/dev/null
meta="$(curl -sS --max-time "$TIMEOUT_SEC" -o "$body_file" -w '%{http_code} %{time_total}' \
  -H 'Content-Type: application/json' \
  -X POST "${BACKEND_URL%/}/v1/videos/generations" \
  -d "$payload" || true)"
status="${meta%% *}"
elapsed="${meta##* }"

if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
  ns_print_error "Video generation failed with HTTP $status after ${elapsed}s"
  head -c 4000 "$body_file"; echo
  exit 1
fi

"$PYTHON" - "$body_file" "$elapsed" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
url = payload.get("video_url") or payload.get("url")
if not url:
    raise SystemExit(f"generation succeeded without video URL: {payload}")
print(f"elapsed={sys.argv[2]}s")
print(f"url={url}")
PY
