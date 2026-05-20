#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

CONTAINERS="nexus-etcd,nexus-lifecycle-manager,nexus-telegram-bot"
WAIT_SECONDS=0
LOG_TAIL=80

usage() {
  cat <<'EOF'
Usage: deploy/scripts/check-essential-containers.sh [--containers CSV] [--wait SECONDS] [--log-tail LINES]

Checks essential Nexus containers and exits non-zero if any are missing, exited,
restarting, or unhealthy. When a container fails, recent logs are printed so the
deploy/operator has an immediate signal.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --containers)
      CONTAINERS="${2:-}"
      shift 2
      ;;
    --wait)
      WAIT_SECONDS="${2:-0}"
      shift 2
      ;;
    --log-tail)
      LOG_TAIL="${2:-80}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_print_error "Unknown option: $1"
      usage >&2
      exit 2
      ;;
  esac
done

IFS=',' read -r -a container_names <<<"$CONTAINERS"

container_status() {
  local name="$1"
  docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true
}

container_health() {
  local name="$1"
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || true
}

print_container_details() {
  local name="$1"
  local status health restarts exit_code error
  status="$(container_status "$name")"
  health="$(container_health "$name")"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo "?")"
  exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$name" 2>/dev/null || echo "?")"
  error="$(docker inspect -f '{{.State.Error}}' "$name" 2>/dev/null || true)"
  printf '%s\tstatus=%s\thealth=%s\trestarts=%s\texit=%s\n' "$name" "${status:-missing}" "${health:-unknown}" "$restarts" "$exit_code"
  if [[ -n "${error:-}" ]]; then
    printf '%s\terror=%s\n' "$name" "$error"
  fi
}

all_ok() {
  local name status health ok="true"
  for name in "${container_names[@]}"; do
    [[ -n "${name:-}" ]] || continue
    status="$(container_status "$name")"
    health="$(container_health "$name")"
    if [[ "$status" != "running" ]]; then
      ok="false"
      continue
    fi
    if [[ "$health" != "none" && "$health" != "healthy" ]]; then
      ok="false"
    fi
  done
  [[ "$ok" == "true" ]]
}

deadline=$((SECONDS + WAIT_SECONDS))
while true; do
  if all_ok; then
    for name in "${container_names[@]}"; do
      [[ -n "${name:-}" ]] || continue
      print_container_details "$name"
    done
    ns_print_ok "Essential containers are running"
    exit 0
  fi

  if (( SECONDS >= deadline )); then
    break
  fi
  sleep 2
done

ns_print_error "One or more essential containers failed to reach a running/healthy state."
for name in "${container_names[@]}"; do
  [[ -n "${name:-}" ]] || continue
  print_container_details "$name"
  status="$(container_status "$name")"
  health="$(container_health "$name")"
  if [[ "$status" != "running" || "$health" == "unhealthy" || "$health" == "starting" || "$status" == "restarting" ]]; then
    ns_print_warn "Recent logs for ${name}:"
    docker logs --tail="$LOG_TAIL" "$name" 2>&1 || true
    if [[ "$health" != "none" ]]; then
      ns_print_warn "Recent healthcheck output for ${name}:"
      docker inspect -f '{{range .State.Health.Log}}{{println .Output}}{{end}}' "$name" 2>/dev/null || true
    fi
  fi
done
exit 1
