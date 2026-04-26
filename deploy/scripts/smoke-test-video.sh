#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd curl

PYTHON="$(ns_pick_python || true)"
[[ -n "${PYTHON:-}" ]] || ns_die "python3/python is required but not installed."

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
BASE_URL="${GATEWAY_BASE_URL:-}"
TOKEN="${GATEWAY_BEARER_TOKEN:-}"
PROMPT="${VIDEO_SMOKE_PROMPT:-A calm foggy mountain valley at sunrise, cinematic, slow camera motion}"
DURATION="${VIDEO_SMOKE_DURATION:-6}"
RESOLUTION="${VIDEO_SMOKE_RESOLUTION:-540p}"
BACKEND_CLASS="${VIDEO_SMOKE_BACKEND_CLASS:-skyreels_v2}"
TIMEOUT_SEC="${VIDEO_SMOKE_TIMEOUT_SEC:-1800}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/smoke-test-video.sh [options]

Run a user-facing video smoke test through Gateway UI auth/session flow.

Options:
  --env-file PATH         Env file path (default: ./.env)
  --base-url URL          Gateway base URL (default: http://127.0.0.1:${GATEWAY_PORT})
  --prompt TEXT           Prompt to submit
  --duration N            Duration in seconds (default: 6)
  --resolution VALUE      Resolution label (default: 540p)
  --backend-class CLASS   Requested backend class (default: skyreels_v2)
  --timeout-sec N         curl max-time for generation request (default: 1800)
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --prompt)
      PROMPT="${2:-}"
      shift 2
      ;;
    --duration)
      DURATION="${2:-}"
      shift 2
      ;;
    --resolution)
      RESOLUTION="${2:-}"
      shift 2
      ;;
    --backend-class)
      BACKEND_CLASS="${2:-}"
      shift 2
      ;;
    --timeout-sec)
      TIMEOUT_SEC="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

if [[ ! -f "${ENV_FILE}" ]]; then
  ns_die "Env file not found: ${ENV_FILE}"
fi

if [[ -z "${BASE_URL}" ]]; then
  gateway_port="$(ns_env_get "${ENV_FILE}" GATEWAY_PORT 8800)"
  BASE_URL="http://127.0.0.1:${gateway_port}"
fi

if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")"
fi
[[ -n "${TOKEN}" ]] || ns_die "GATEWAY_BEARER_TOKEN is not set (set env var or put it in ${ENV_FILE})."

cookie_jar="$(mktemp)"
session_body="$(mktemp)"
backend_body="$(mktemp)"
video_body="$(mktemp)"
trap 'rm -f "$cookie_jar" "$session_body" "$backend_body" "$video_body"' EXIT

print_body_preview() {
  local body_file="$1"
  local limit="${2:-1200}"
  if [[ -s "$body_file" ]]; then
    head -c "$limit" "$body_file"
    echo
  fi
}

handle_auth_failure() {
  local status="$1"
  local body_file="$2"
  ns_print_error "Video smoke auth failed with HTTP ${status}."
  print_body_preview "$body_file"
  if [[ "$status" == "403" ]]; then
    ns_print_warn "UI auth routes are IP-allowlisted. Run this on the gateway host or allowlist your client IP/CIDR."
  elif [[ "$status" == "401" ]]; then
    ns_print_warn "Gateway rejected the API key. Verify GATEWAY_BEARER_TOKEN in ${ENV_FILE}."
  fi
}

ns_print_header "Video Smoke Test"
echo "Base URL: ${BASE_URL}"
echo "Backend: ${BACKEND_CLASS}"
echo "Duration: ${DURATION}s"
echo "Resolution: ${RESOLUTION}"

echo "[1/3] Create UI session from API key"
session_status="$(curl -sS -o "$session_body" -c "$cookie_jar" -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -X POST "${BASE_URL%/}/ui/api/auth/api-key-session" || true)"
if [[ "$session_status" != "200" ]]; then
  handle_auth_failure "$session_status" "$session_body"
  exit 1
fi

echo "[2/3] Check video backend availability"
backend_status="$(curl -sS -o "$backend_body" -b "$cookie_jar" -w "%{http_code}" \
  "${BASE_URL%/}/ui/api/video/backends" || true)"
if [[ "$backend_status" != "200" ]]; then
  ns_print_error "Failed to query video backends (HTTP ${backend_status})."
  print_body_preview "$backend_body"
  exit 1
fi

"$PYTHON" - "$backend_body" "$BACKEND_CLASS" <<'PY'
import json, sys
path, backend_class = sys.argv[1], sys.argv[2]
payload = json.load(open(path, "r", encoding="utf-8"))
items = payload.get("available_backends") or payload.get("backends") or []
match = None
for item in items:
    if str(item.get("backend_class") or "").strip() == backend_class:
        match = item
        break
if match is None:
    raise SystemExit(f"Requested video backend is not available: {backend_class}")
healthy = match.get("healthy")
ready = match.get("ready")
if healthy is False or ready is False:
    raise SystemExit(f"Requested video backend is not healthy/ready: {backend_class} healthy={healthy} ready={ready}")
desc = str(match.get("description") or backend_class)
print(f"backend_ok={backend_class} description={desc}")
PY

request_payload="$("$PYTHON" - "$PROMPT" "$DURATION" "$RESOLUTION" "$BACKEND_CLASS" <<'PY'
import json, sys
prompt, duration, resolution, backend_class = sys.argv[1:5]
body = {
    "prompt": prompt,
    "duration": int(duration),
    "resolution": resolution,
}
if backend_class:
    body["backend_class"] = backend_class
print(json.dumps(body, ensure_ascii=False))
PY
)"

echo "[3/3] POST /ui/api/video"
video_meta="$(curl -sS --max-time "$TIMEOUT_SEC" \
  -o "$video_body" \
  -b "$cookie_jar" \
  -w "%{http_code} %{time_total}" \
  -H "Content-Type: application/json" \
  -X POST "${BASE_URL%/}/ui/api/video" \
  -d "$request_payload" || true)"
video_status="${video_meta%% *}"
video_elapsed="${video_meta##* }"

if [[ ! "$video_status" =~ ^2[0-9][0-9]$ ]]; then
  ns_print_error "Video smoke request failed with HTTP ${video_status} after ${video_elapsed}s."
  "$PYTHON" - "$video_body" <<'PY'
import json, sys
path = sys.argv[1]
text = open(path, "r", encoding="utf-8", errors="ignore").read()
try:
    payload = json.loads(text)
except Exception:
    print(text[:2000])
    raise SystemExit(0)
detail = payload.get("detail")
if isinstance(detail, dict):
    body = detail.get("body")
    if isinstance(body, dict):
        stderr = str(body.get("stderr") or "").strip()
        stdout = str(body.get("stdout") or "").strip()
        if body.get("error"):
            print(f"upstream_error={body.get('error')}")
        if body.get("returncode") is not None:
            print(f"returncode={body.get('returncode')}")
        if stderr:
            print("stderr:")
            print(stderr[-2000:])
        if stdout:
            print("stdout:")
            print(stdout[-2000:])
        raise SystemExit(0)
print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
PY
  exit 1
fi

"$PYTHON" - "$video_body" "$video_elapsed" <<'PY'
import json, sys
path, elapsed = sys.argv[1], sys.argv[2]
payload = json.load(open(path, "r", encoding="utf-8"))
url = payload.get("url") or payload.get("video_url")
if not url and isinstance(payload.get("data"), list):
    for item in payload["data"]:
        if isinstance(item, dict) and item.get("url"):
            url = item["url"]
            break
videos = payload.get("videos")
status = str(payload.get("status") or "")
if url:
    print(f"elapsed={elapsed}s")
    print(f"url={url}")
    raise SystemExit(0)
if status == "ok" and isinstance(videos, list) and videos:
    print(f"elapsed={elapsed}s")
    print("videos=" + ", ".join(str(item) for item in videos))
    raise SystemExit(0)
print(json.dumps(payload, ensure_ascii=False, indent=2))
raise SystemExit("Video smoke response did not include a usable success payload.")
PY

ns_print_ok "Video smoke test passed"
