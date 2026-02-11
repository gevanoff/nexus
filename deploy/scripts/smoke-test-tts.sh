#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd curl

BASE_URL="${TTS_BASE_URL:-http://127.0.0.1:${TTS_PORT:-9940}}"

ns_print_header "TTS Smoke Test"
echo "Base URL: ${BASE_URL}"

echo "[1/3] GET /health"
curl -fsS "${BASE_URL}/health" >/dev/null

echo "[2/3] GET /v1/models"
curl -fsS "${BASE_URL}/v1/models" >/dev/null

echo "[3/3] POST /v1/audio/speech (wav)"
curl -fsS -X POST "${BASE_URL}/v1/audio/speech" \
  -H "Content-Type: application/json" \
  -d '{"input":"smoke test","voice":"alba","response_format":"wav"}' \
  --output /tmp/nexus-tts-smoke.wav

ns_print_ok "TTS smoke tests passed (wrote /tmp/nexus-tts-smoke.wav)"
