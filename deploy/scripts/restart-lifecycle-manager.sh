#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
NO_BUILD="false"
PROFILE="default"
TARGET_USER=""
COLIMA_RUNTIME_ROOT="${COLIMA_RUNTIME_ROOT:-/var/lib/nexus-colima}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/restart-lifecycle-manager.sh [--env-file PATH] [--no-build] [--profile NAME] [--user USER] [--runtime-root PATH]

Restart only the Lifecycle Manager service. On macOS, if a Colima LaunchDaemon
env file exists, this helper loads its managed COLIMA_HOME/HOME/DOCKER_HOST so
the restart targets the same Docker daemon as the boot-managed Colima runtime.

Options:
  --env-file PATH     Env file path (default: ./.env)
  --no-build          Skip image rebuild (recreate lifecycle-manager from the existing image)
  --profile NAME      Colima profile name used by the launchd helper (default: default)
  --user USER         User account that owns the managed Colima runtime (default: current user)
  --runtime-root PATH Root-owned Colima helper asset directory (default: /var/lib/nexus-colima)
EOF
}

sanitize_token() {
  printf '%s' "${1:-}" | tr -c 'A-Za-z0-9._-' '_'
}

resolve_target_user() {
  if [[ -n "${TARGET_USER:-}" ]]; then
    printf '%s\n' "$TARGET_USER"
    return 0
  fi

  if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    printf '%s\n' "$SUDO_USER"
    return 0
  fi

  id -un
}

apply_managed_colima_env() {
  local profile_name="$1"
  local user_name="$2"

  [[ "$(ns_detect_platform)" == "macos" ]] || return 0

  local sanitized_profile sanitized_user env_path docker_socket alt_socket
  sanitized_profile="$(sanitize_token "$profile_name")"
  sanitized_user="$(sanitize_token "$user_name")"
  env_path="${COLIMA_RUNTIME_ROOT}/${sanitized_user}-${sanitized_profile}.env"

  if [[ ! -f "$env_path" ]]; then
    return 0
  fi

  # shellcheck source=/dev/null
  source "$env_path"

  if [[ -n "${DOCKER_BIN:-}" ]]; then
    ns_append_path_dir "$(dirname "$DOCKER_BIN")"
  fi
  if [[ -n "${COLIMA_BIN:-}" ]]; then
    ns_append_path_dir "$(dirname "$COLIMA_BIN")"
  fi

  if [[ -n "${COLIMA_USER_HOME:-}" ]]; then
    export HOME="$COLIMA_USER_HOME"
  fi

  if [[ -n "${COLIMA_HOME:-}" ]]; then
    export COLIMA_HOME
    docker_socket="${COLIMA_HOME}/default/docker.sock"
    alt_socket="${docker_socket/#\/ai-data/\/Volumes\/ai_data}"
    if [[ -S "$docker_socket" ]]; then
      export DOCKER_HOST="unix://${docker_socket}"
    elif [[ -S "$alt_socket" ]]; then
      export DOCKER_HOST="unix://${alt_socket}"
    fi
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --no-build)
      NO_BUILD="true"
      shift
      ;;
    --profile)
      PROFILE="${2:-}"
      shift 2
      ;;
    --user)
      TARGET_USER="${2:-}"
      shift 2
      ;;
    --runtime-root)
      COLIMA_RUNTIME_ROOT="${2:-}"
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

[[ -n "${PROFILE:-}" ]] || ns_die "--profile must not be empty"
[[ "${COLIMA_RUNTIME_ROOT}" == /* ]] || ns_die "--runtime-root must be an absolute path"

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

TARGET_USER="$(resolve_target_user)"
apply_managed_colima_env "$PROFILE" "$TARGET_USER"

ns_ensure_project_env_bind_source "$ROOT_DIR" "$ENV_FILE"

if ! ns_compose_available; then
  ns_die "Docker Compose is not available"
fi
if ! ns_ensure_docker_daemon true; then
  ns_die "Docker daemon is not reachable"
fi

runtime_root="$(ns_runtime_root "$ROOT_DIR")"
ns_mkdir_p "${runtime_root}/lifecycle-manager/ssh"
ns_mkdir_p "${runtime_root}/lifecycle-manager/state"
ns_verify_docker_bind_source "$ROOT_DIR"
ns_verify_docker_bind_source "$ROOT_DIR/.env"
ns_verify_docker_bind_source "${runtime_root}/lifecycle-manager/state"

ns_print_header "Restarting Lifecycle Manager"
if ! ns_compose --env-file "$ENV_FILE" -f docker-compose.lifecycle-manager.yml config >/dev/null 2>&1; then
  ns_print_error "Compose failed to parse $ENV_FILE"
  ns_print_warn "Check for malformed variable syntax (for example an unmatched \${...} expression)."
  exit 1
fi

if [[ "$NO_BUILD" == "true" ]]; then
  ns_compose --env-file "$ENV_FILE" -f docker-compose.lifecycle-manager.yml up -d --force-recreate lifecycle-manager
else
  ns_compose --env-file "$ENV_FILE" -f docker-compose.lifecycle-manager.yml up -d --build --force-recreate lifecycle-manager
fi

lifecycle_port="$(ns_env_get "$ENV_FILE" LIFECYCLE_MANAGER_PORT 9190)"
ready_url="http://127.0.0.1:${lifecycle_port}/readyz"

ns_print_header "Waiting for Lifecycle Manager readiness"
for i in {1..60}; do
  if curl -fsS "$ready_url" >/dev/null 2>&1; then
    ns_print_ok "Lifecycle Manager ready endpoint is up (${ready_url})"
    exit 0
  fi
  sleep 2
done

ns_print_error "Lifecycle Manager did not become ready in time (${ready_url})"
ns_compose --env-file "$ENV_FILE" -f docker-compose.lifecycle-manager.yml ps || true
ns_compose --env-file "$ENV_FILE" -f docker-compose.lifecycle-manager.yml logs --tail=120 lifecycle-manager || true
exit 1