#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NEXUS_DIR="${NEXUS_DIR:-$ROOT_DIR}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/gateway-backups}"
COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml)
WITH_MLX="false"

if [[ "$(ns_detect_platform)" == "macos" ]] && [[ "${EUID:-$(id -u)}" -eq 0 ]] && ns_have_cmd colima; then
  ns_die "Do not run this script with sudo on macOS when using Colima. Run as a normal user and let individual commands use sudo."
fi

usage() {
  cat <<'EOF'
Usage: deploy/scripts/cutover-one-way.sh [--with-mlx]

One-way host-local cutover from legacy ai-infra launchd services to Nexus containers.

Env vars (optional):
  NEXUS_DIR    Nexus repo root (default: current repo root)
  BACKUP_DIR   Where to write legacy gateway data backups

Options:
  --with-mlx   Include MLX component (docker-compose.mlx.yml) during cutover
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-mlx)
      WITH_MLX="true"
      shift
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

if [[ "$WITH_MLX" == "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.mlx.yml)
fi

ns_print_header "One-way cutover: legacy -> Nexus"

ns_print_header "Stopping legacy launchd services"
if [[ -d "$HOME/ai/ai-infra/services/gateway" ]]; then
  (cd "$HOME/ai/ai-infra/services/gateway" && ./scripts/uninstall.sh) || true
fi
if [[ -d "$HOME/ai/ai-infra/services/mlx" ]]; then
  (cd "$HOME/ai/ai-infra/services/mlx" && ./scripts/uninstall.sh) || true
fi

sudo launchctl bootout system/com.ai.gateway 2>/dev/null || true
sudo launchctl bootout system/com.ai.gateway.redirect 2>/dev/null || true
sudo launchctl bootout system/com.mlx.openai.server 2>/dev/null || true

sudo launchctl disable system/com.ai.gateway 2>/dev/null || true
sudo launchctl disable system/com.ai.gateway.redirect 2>/dev/null || true
sudo launchctl disable system/com.mlx.openai.server 2>/dev/null || true

sudo rm -f /Library/LaunchDaemons/com.ai.gateway.plist
sudo rm -f /Library/LaunchDaemons/com.ai.gateway.redirect.plist
sudo rm -f /Library/LaunchDaemons/com.mlx.openai.server.plist

ns_print_header "Backing up legacy gateway data (best-effort)"
mkdir -p "$BACKUP_DIR"
if [[ -d /var/lib/gateway/data ]]; then
  ts="$(date +%Y%m%d-%H%M%S)"
  sudo tar -czf "$BACKUP_DIR/gateway-data-$ts.tgz" /var/lib/gateway/data 2>/dev/null || true
  ns_print_ok "Backup attempt complete: $BACKUP_DIR/gateway-data-$ts.tgz"
else
  ns_print_warn "No /var/lib/gateway/data found; skipping backup"
fi

ns_print_header "Checking runtime prerequisites"
ns_ensure_prereqs true true false false false false || true
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is still not reachable after auto-start attempts."
fi
if ! ns_compose_available; then
  ns_die "Docker Compose is not available (need either 'docker compose' or 'docker-compose')."
fi

cd "$NEXUS_DIR"

ns_print_header "Preparing Nexus configuration"
ns_ensure_env_file "$NEXUS_DIR/.env" "$NEXUS_DIR"
ns_ensure_runtime_dirs "$NEXUS_DIR"
ns_seed_gateway_config_files "$NEXUS_DIR"
ns_verify_docker_bind_source "$NEXUS_DIR"
ns_verify_docker_bind_source "$NEXUS_DIR/.env"

ns_print_header "Running Nexus preflight"
if [[ -x "$NEXUS_DIR/deploy/scripts/preflight-check.sh" ]]; then
  "$NEXUS_DIR/deploy/scripts/preflight-check.sh" --mode quickstart --env-file "$NEXUS_DIR/.env"
fi

ns_print_header "Starting Nexus core stack"
ns_compose --env-file "$NEXUS_DIR/.env" "${COMPOSE_ARGS[@]}" up -d --build

ns_print_header "Waiting for gateway health"
for i in {1..60}; do
  if curl -fsS http://127.0.0.1:8800/health >/dev/null 2>&1; then
    ns_print_ok "Gateway health endpoint is up"
    break
  fi
  sleep 2
  if [[ "$i" -eq 60 ]]; then
    ns_print_error "Gateway did not become healthy in time"
    ns_compose "${COMPOSE_ARGS[@]}" ps || true
    ns_compose "${COMPOSE_ARGS[@]}" logs --tail=120 gateway || true
    exit 1
  fi
done

ns_print_header "Verifying gateway contract"
if [[ "$WITH_MLX" == "true" ]]; then
  "$NEXUS_DIR/deploy/scripts/verify-gateway.sh" --with-mlx
else
  "$NEXUS_DIR/deploy/scripts/verify-gateway.sh"
fi

ns_print_header "Cutover complete"
ns_compose "${COMPOSE_ARGS[@]}" ps
