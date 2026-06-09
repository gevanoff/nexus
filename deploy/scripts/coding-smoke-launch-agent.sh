#!/usr/bin/env bash
set -euo pipefail
umask 077

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

ENV_FILE="${NEXUS_CODING_SMOKE_ENV_FILE:-/ai-data/var/lib/nexus-smoke/coding/coding-smoke.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi

REPO_DIR="${NEXUS_REPO_DIR:-/ai-data/var/lib/nexus}"
NEXUS_ENV_FILE="${NEXUS_ENV_FILE:-${REPO_DIR}/.env}"
NEXUS_GATEWAY_URL="${NEXUS_GATEWAY_URL:-http://127.0.0.1:8800}"
NEXUS_CODING_SMOKE_OUTPUT_DIR="${NEXUS_CODING_SMOKE_OUTPUT_DIR:-/ai-data/var/lib/nexus-smoke/coding}"
NEXUS_CODING_SMOKE_MODELS="${NEXUS_CODING_SMOKE_MODELS:-coder}"
NEXUS_CODING_SMOKE_WEEKLY_MODELS="${NEXUS_CODING_SMOKE_WEEKLY_MODELS:-}"
NEXUS_CODING_SMOKE_WEEKLY_DAY="${NEXUS_CODING_SMOKE_WEEKLY_DAY:-7}"
NEXUS_CODING_SMOKE_IDLE_START_HOUR="${NEXUS_CODING_SMOKE_IDLE_START_HOUR:-0}"
NEXUS_CODING_SMOKE_IDLE_END_HOUR="${NEXUS_CODING_SMOKE_IDLE_END_HOUR:-6}"
NEXUS_CODING_SMOKE_TIMEOUT_SEC="${NEXUS_CODING_SMOKE_TIMEOUT_SEC:-1200}"
NEXUS_CODING_SMOKE_POLL_SEC="${NEXUS_CODING_SMOKE_POLL_SEC:-10}"
NEXUS_CODING_SMOKE_STALLED_AFTER_SEC="${NEXUS_CODING_SMOKE_STALLED_AFTER_SEC:-180}"
NEXUS_CODING_SMOKE_PROFILE_ID="${NEXUS_CODING_SMOKE_PROFILE_ID:-fixture_median}"
NEXUS_CODING_SMOKE_PROFILE_LABEL="${NEXUS_CODING_SMOKE_PROFILE_LABEL:-Fixture median repair}"
NEXUS_CODING_SMOKE_COMPLEXITY="${NEXUS_CODING_SMOKE_COMPLEXITY:-simple}"
NEXUS_CODING_SMOKE_STDOUT_LOG="${NEXUS_CODING_SMOKE_STDOUT_LOG:-}"
NEXUS_CODING_SMOKE_STDERR_LOG="${NEXUS_CODING_SMOKE_STDERR_LOG:-}"
LOCK_DIR="${NEXUS_CODING_SMOKE_LOCK_DIR:-${NEXUS_CODING_SMOKE_OUTPUT_DIR}/.lock}"

export NEXUS_GATEWAY_URL
export NEXUS_CODING_SMOKE_OUTPUT_DIR

if [[ -n "$NEXUS_CODING_SMOKE_STDOUT_LOG" || -n "$NEXUS_CODING_SMOKE_STDERR_LOG" ]]; then
  if [[ -n "$NEXUS_CODING_SMOKE_STDOUT_LOG" ]]; then
    mkdir -p "$(dirname "$NEXUS_CODING_SMOKE_STDOUT_LOG")"
    exec >>"$NEXUS_CODING_SMOKE_STDOUT_LOG"
  fi
  if [[ -n "$NEXUS_CODING_SMOKE_STDERR_LOG" ]]; then
    mkdir -p "$(dirname "$NEXUS_CODING_SMOKE_STDERR_LOG")"
    exec 2>>"$NEXUS_CODING_SMOKE_STDERR_LOG"
  fi
fi

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '%s %s\n' "$(timestamp)" "$*"
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

in_weekly_idle_window() {
  local day hour
  day="$(date '+%u')"
  hour="$(date '+%H')"
  [[ "$day" == "$NEXUS_CODING_SMOKE_WEEKLY_DAY" ]] || return 1
  (( 10#$hour >= NEXUS_CODING_SMOKE_IDLE_START_HOUR && 10#$hour < NEXUS_CODING_SMOKE_IDLE_END_HOUR ))
}

run_one_model() {
  local model="$1"
  [[ -n "$model" ]] || return 0
  log "Starting Coding smoke model=${model} profile=${NEXUS_CODING_SMOKE_PROFILE_ID}"
  "$REPO_DIR/deploy/scripts/run-coding-smoke-test.sh" \
    --base-url "$NEXUS_GATEWAY_URL" \
    --env-file "$NEXUS_ENV_FILE" \
    --model "$model" \
    --timeout-sec "$NEXUS_CODING_SMOKE_TIMEOUT_SEC" \
    --poll-sec "$NEXUS_CODING_SMOKE_POLL_SEC" \
    --stalled-after-sec "$NEXUS_CODING_SMOKE_STALLED_AFTER_SEC" \
    --profile-id "$NEXUS_CODING_SMOKE_PROFILE_ID" \
    --profile-label "$NEXUS_CODING_SMOKE_PROFILE_LABEL" \
    --complexity "$NEXUS_CODING_SMOKE_COMPLEXITY"
}

if [[ ! -x "$REPO_DIR/deploy/scripts/run-coding-smoke-test.sh" ]]; then
  log "ERROR: smoke wrapper not executable: $REPO_DIR/deploy/scripts/run-coding-smoke-test.sh"
  exit 1
fi

mkdir -p "$NEXUS_CODING_SMOKE_OUTPUT_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [[ -f "$LOCK_DIR/pid" ]]; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
      log "Coding smoke already running pid=${old_pid}; skipping"
      exit 0
    fi
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
trap 'rm -rf "$LOCK_DIR"' EXIT
printf '%s\n' "$$" >"$LOCK_DIR/pid"

status=0
old_ifs="$IFS"
IFS=","
read -r -a models <<<"$NEXUS_CODING_SMOKE_MODELS"
IFS="$old_ifs"
for raw_model in "${models[@]}"; do
  model="$(trim "$raw_model")"
  [[ -n "$model" ]] || continue
  if ! run_one_model "$model"; then
    status=1
  fi
done

if [[ -n "$NEXUS_CODING_SMOKE_WEEKLY_MODELS" ]]; then
  if in_weekly_idle_window; then
    old_ifs="$IFS"
    IFS=","
    read -r -a weekly_models <<<"$NEXUS_CODING_SMOKE_WEEKLY_MODELS"
    IFS="$old_ifs"
    for raw_model in "${weekly_models[@]}"; do
      model="$(trim "$raw_model")"
      [[ -n "$model" ]] || continue
      if ! run_one_model "$model"; then
        status=1
      fi
    done
  else
    log "Weekly Coding smoke models skipped outside idle window"
  fi
fi

log "Coding smoke completed status=${status}"
exit "$status"
