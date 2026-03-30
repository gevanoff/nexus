#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
TELEGRAM_COMPOSE_FILE="${TELEGRAM_COMPOSE_FILE:-docker-compose.telegram-bot.yml}"
RUNTIME_ENV="${TELEGRAM_RUNTIME_ENV:-/var/lib/telegram-bot/telegram-bot.env}"
RUNTIME_APP="${TELEGRAM_RUNTIME_APP:-/var/lib/telegram-bot/app/telegram_gateway_bot.js}"

rc=0

mark_fail() {
  rc=1
}

mask_secret() {
  local v="${1:-}"
  if [[ -z "$v" ]]; then
    echo ""
    return 0
  fi
  local n=${#v}
  if (( n <= 6 )); then
    echo "***"
    return 0
  fi
  local tail="${v: -4}"
  echo "***${tail}"
}

http_status() {
  local url="$1"
  if [[ "${2:-}" == "auth" ]]; then
    local tok="${3:-}"
    curl -sS -m 10 -o /dev/null -w "%{http_code}" "$url" -H "Authorization: Bearer ${tok}" 2>/dev/null || true
    return 0
  fi
  curl -sS -m 10 -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || true
}

ns_print_header "Telegram Bot Diagnostics"
echo "Repo root: ${ROOT_DIR}"
echo "Nexus env file: ${ENV_FILE}"
echo "Nexus compose file: ${TELEGRAM_COMPOSE_FILE}"
echo "Bot runtime env: ${RUNTIME_ENV}"

echo
ns_print_header "Configuration sources"

nexus_telegram_token=""
nexus_gateway_token=""
nexus_gateway_port="8800"
nexus_obs_port="8801"

if [[ -f "$ENV_FILE" ]]; then
  ns_print_ok "Found Nexus env: ${ENV_FILE}"
  nexus_telegram_token="$(ns_env_get "$ENV_FILE" TELEGRAM_TOKEN "")"
  nexus_gateway_token="$(ns_env_get "$ENV_FILE" GATEWAY_BEARER_TOKEN "")"
  nexus_gateway_port="$(ns_env_get "$ENV_FILE" GATEWAY_PORT "8800")"
  nexus_obs_port="$(ns_env_get "$ENV_FILE" OBSERVABILITY_PORT "8801")"
else
  ns_print_warn "Nexus env file not found at ${ENV_FILE}"
fi

runtime_telegram_token=""
runtime_gateway_token=""
runtime_gateway_port=""

if [[ -f "$RUNTIME_ENV" ]]; then
  ns_print_ok "Found telegram runtime env: ${RUNTIME_ENV}"
  runtime_telegram_token="$(ns_env_get "$RUNTIME_ENV" TELEGRAM_TOKEN "")"
  runtime_gateway_token="$(ns_env_get "$RUNTIME_ENV" GATEWAY_BEARER_TOKEN "")"
  runtime_gateway_port="$(ns_env_get "$RUNTIME_ENV" GATEWAY_PORT "")"
else
  ns_print_warn "Telegram runtime env not found at ${RUNTIME_ENV}"
fi

if [[ -f "$ROOT_DIR/$TELEGRAM_COMPOSE_FILE" ]]; then
  ns_print_ok "Found Nexus telegram compose file: ${TELEGRAM_COMPOSE_FILE}"
else
  ns_print_warn "Nexus telegram compose file not found: ${TELEGRAM_COMPOSE_FILE}"
fi

effective_telegram_token="$runtime_telegram_token"
if [[ -z "$effective_telegram_token" ]]; then
  effective_telegram_token="$nexus_telegram_token"
fi

effective_gateway_token="$runtime_gateway_token"
if [[ -z "$effective_gateway_token" ]]; then
  effective_gateway_token="$nexus_gateway_token"
fi

effective_gateway_port="$runtime_gateway_port"
if [[ -z "$effective_gateway_port" ]]; then
  effective_gateway_port="$nexus_gateway_port"
fi

if [[ -n "$nexus_telegram_token" ]]; then
  ns_print_ok "Nexus TELEGRAM_TOKEN is set: $(mask_secret "$nexus_telegram_token")"
else
  ns_print_warn "Nexus TELEGRAM_TOKEN is not set"
fi

if [[ -n "$runtime_telegram_token" ]]; then
  ns_print_ok "Runtime TELEGRAM_TOKEN is set: $(mask_secret "$runtime_telegram_token")"
else
  ns_print_warn "Runtime TELEGRAM_TOKEN is not set"
fi

if [[ -n "$effective_telegram_token" ]]; then
  ns_print_ok "Effective TELEGRAM_TOKEN selected: $(mask_secret "$effective_telegram_token")"
else
  ns_print_error "No TELEGRAM_TOKEN available from runtime or Nexus env"
  mark_fail
fi

if [[ -n "$effective_gateway_token" ]]; then
  ns_print_ok "Effective GATEWAY_BEARER_TOKEN selected: $(mask_secret "$effective_gateway_token")"
else
  ns_print_warn "No effective GATEWAY_BEARER_TOKEN found (bot chats will fail auth)"
fi

echo
ns_print_header "Telegram bot runtime"

if [[ -f "$RUNTIME_APP" ]]; then
  ns_print_ok "Bot app file exists: ${RUNTIME_APP}"
else
  ns_print_warn "Bot app file missing: ${RUNTIME_APP}"
fi

if ns_have_cmd pgrep; then
  if pgrep -af "telegram_gateway_bot.js" >/dev/null 2>&1; then
    ns_print_ok "Bot process appears to be running"
    pgrep -af "telegram_gateway_bot.js" | head -n 3
  else
    ns_print_warn "No running process matching telegram_gateway_bot.js"
  fi
else
  ns_print_warn "pgrep not available; skipping process check"
fi

if ns_have_cmd systemctl; then
  if systemctl list-unit-files >/dev/null 2>&1; then
    if systemctl is-active --quiet telegram-bot >/dev/null 2>&1; then
      ns_print_ok "systemd service telegram-bot is active"
    else
      ns_print_warn "systemd service telegram-bot is not active"
    fi
  else
    ns_print_warn "systemctl present but systemd bus is unavailable in this shell"
  fi
elif ns_have_cmd launchctl; then
  if launchctl print system/com.telegram-bot.server >/dev/null 2>&1; then
    ns_print_ok "launchd service com.telegram-bot.server exists"
  else
    ns_print_warn "launchd service com.telegram-bot.server not found"
  fi
else
  ns_print_warn "No systemd/launchd tool found; skipping service check"
fi

if ns_compose_available && [[ -f "$ROOT_DIR/$TELEGRAM_COMPOSE_FILE" ]]; then
  if ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f "$TELEGRAM_COMPOSE_FILE" ps telegram-bot >/dev/null 2>&1; then
    ns_print_ok "Compose service telegram-bot is defined"
    ns_compose --env-file "$ENV_FILE" -f docker-compose.gateway.yml -f "$TELEGRAM_COMPOSE_FILE" ps telegram-bot || true
  else
    ns_print_warn "Compose could not resolve telegram-bot service with ${TELEGRAM_COMPOSE_FILE}"
    ns_print_warn "Try: docker compose --env-file .env -f docker-compose.gateway.yml -f ${TELEGRAM_COMPOSE_FILE} up -d --build"
  fi
fi

echo
ns_print_header "External Telegram API check"

if [[ -n "$effective_telegram_token" ]]; then
  tmp="$(mktemp)"
  tg_status="$(curl -sS -m 10 -o "$tmp" -w "%{http_code}" "https://api.telegram.org/bot${effective_telegram_token}/getMe" || true)"
  if [[ "$tg_status" == "200" ]] && grep -q '"ok"[[:space:]]*:[[:space:]]*true' "$tmp"; then
    ns_print_ok "Telegram token is valid (getMe ok=true)"
  else
    ns_print_error "Telegram token check failed (HTTP ${tg_status})"
    if [[ -s "$tmp" ]]; then
      ns_print_warn "Telegram response preview:"
      head -c 400 "$tmp"
      echo
    fi
    mark_fail
  fi
  rm -f "$tmp"
else
  ns_print_error "Skipping Telegram API check (no effective token)"
  mark_fail
fi

echo
ns_print_header "Gateway connectivity check"

obs_url="http://127.0.0.1:${nexus_obs_port}/health"
models_url="https://127.0.0.1:${effective_gateway_port}/v1/models"

obs_status="$(http_status "$obs_url")"
if [[ "$obs_status" == "200" ]]; then
  ns_print_ok "Gateway observability health reachable (${obs_url})"
else
  ns_print_warn "Gateway observability health returned HTTP ${obs_status} (${obs_url})"
fi

if [[ -n "$effective_gateway_token" ]]; then
  models_status="$(curl -sS -k -m 10 -o /dev/null -w "%{http_code}" "$models_url" -H "Authorization: Bearer ${effective_gateway_token}" 2>/dev/null || true)"
  if [[ "$models_status" =~ ^2[0-9][0-9]$ ]]; then
    ns_print_ok "Gateway chat API auth check passed (${models_url})"
  else
    ns_print_warn "Gateway chat API auth check returned HTTP ${models_status} (${models_url})"
    ns_print_warn "If this fails, Telegram bot replies will fail even with a valid TELEGRAM_TOKEN."
  fi
else
  ns_print_warn "Skipping gateway auth check (missing effective GATEWAY_BEARER_TOKEN)"
fi

echo
ns_print_header "Summary"
if [[ "$rc" -eq 0 ]]; then
  ns_print_ok "No blocking issues detected by telegram diagnostics"
else
  ns_print_error "Telegram diagnostics found issues"
fi

if [[ -f "$ROOT_DIR/$TELEGRAM_COMPOSE_FILE" ]]; then
  ns_print_warn "If telegram-bot is enabled in Nexus, ensure .env has TELEGRAM_TOKEN and GATEWAY_BEARER_TOKEN then run:"
  ns_print_warn "  docker compose --env-file .env -f docker-compose.gateway.yml -f ${TELEGRAM_COMPOSE_FILE} up -d --build"
else
  ns_print_warn "If you installed telegram-bot separately (legacy host service), ensure its runtime env has the right tokens and service is active."
fi

exit "$rc"
