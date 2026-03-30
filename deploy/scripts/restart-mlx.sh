#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

MLX_ENV_FILE="${MLX_ENV_FILE:-/var/lib/mlx/mlx.env}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.nexus.mlx.openai.server}"
TIMEOUT_SEC="${TIMEOUT_SEC:-60}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restart-mlx.sh [--mlx-env-file PATH] [--label LABEL] [--timeout-sec N]

Restart the native macOS MLX launchd service.

If the launchd job is not currently loaded but its plist exists, this script
bootstraps it back into the system domain before restarting it.

Options:
  --mlx-env-file PATH  MLX runtime env file (default: /var/lib/mlx/mlx.env)
  --label LABEL        launchd label (default: com.nexus.mlx.openai.server)
  --timeout-sec N      Wait time for /v1/models health (default: 60)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mlx-env-file)
      MLX_ENV_FILE="${2:-}"
      shift 2
      ;;
    --label)
      LAUNCHD_LABEL="${2:-}"
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

if [[ ! "$TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  ns_die "Invalid --timeout-sec value: $TIMEOUT_SEC"
fi

if ! ns_have_cmd sudo; then
  ns_die "sudo is required"
fi
if ! ns_have_cmd launchctl; then
  ns_die "launchctl is required"
fi
if ! ns_have_cmd curl; then
  ns_die "curl is required"
fi

PLIST_PATH="/Library/LaunchDaemons/${LAUNCHD_LABEL}.plist"
mlx_host="$(ns_env_get "$MLX_ENV_FILE" MLX_HOST "127.0.0.1")"
mlx_port="$(ns_env_get "$MLX_ENV_FILE" MLX_PORT "10240")"
mlx_log_dir="$(ns_env_get "$MLX_ENV_FILE" MLX_LOG_DIR "/var/log/mlx")"

if [[ "$mlx_host" == "0.0.0.0" || -z "$mlx_host" ]]; then
  health_host="127.0.0.1"
else
  health_host="$mlx_host"
fi
health_url="http://${health_host}:${mlx_port}/v1/models"

ns_print_header "Restarting Native MLX"

if sudo launchctl print "system/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
  ns_print_ok "launchd job is loaded: ${LAUNCHD_LABEL}"
else
  if [[ -f "$PLIST_PATH" ]]; then
    ns_print_warn "launchd job is not loaded; bootstrapping ${PLIST_PATH}"
    sudo launchctl bootstrap system "$PLIST_PATH"
  else
    ns_print_error "MLX launchd plist not found: ${PLIST_PATH}"
    ns_print_warn "Install or reinstall the native MLX service first:"
    ns_print_warn "  ./services/mlx/scripts/install-native-macos.sh --config /var/lib/mlx/config/config.yaml"
    exit 1
  fi
fi

sudo launchctl kickstart -k "system/${LAUNCHD_LABEL}"

ns_print_header "Waiting for MLX health"
for ((i=0; i<TIMEOUT_SEC; i++)); do
  if curl -fsS "$health_url" >/dev/null 2>&1; then
    ns_print_ok "MLX responded at ${health_url}"
    exit 0
  fi
  sleep 1
done

ns_print_error "MLX did not become healthy in time (${health_url})"
sudo launchctl print "system/${LAUNCHD_LABEL}" || true
sudo tail -n 120 "${mlx_log_dir}/mlx-openai.err.log" || true
sudo tail -n 120 "${mlx_log_dir}/mlx-openai.out.log" || true
exit 1
