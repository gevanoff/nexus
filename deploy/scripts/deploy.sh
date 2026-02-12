#!/usr/bin/env bash
set -euo pipefail
umask 077

# Maintainer note:
# Keep cross-script logic in deploy/scripts/_common.sh (prereqs, env files, prompts,
# validation helpers). Avoid copy/paste changes across scripts.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"
ENV_FILE=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/deploy.sh [--yes] [--env-file PATH] <environment> <branch>

Suggested order (typical):
  1) ./deploy/scripts/install-host-deps.sh
  2) ./deploy/scripts/import-env.sh   (or: cp .env.example .env)
  3) ./deploy/scripts/preflight-check.sh --mode deploy
  4) ./deploy/scripts/deploy.sh dev main   (or prod)
  5) ./deploy/scripts/verify-gateway.sh

Arguments:
  environment: dev | prod
  branch: git branch to deploy (e.g., dev or main)

Options:
  --yes            Non-interactive (assume "yes" for install prompts)
  --env-file PATH  Env file to use (default: deploy/env/.env.<environment> if present, else ./.env)
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
        ;;
      --env-file)
        ENV_FILE="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        ns_print_error "Unknown option: $1"
        usage
        exit 2
        ;;
      *)
        break
        ;;
    esac
  done

  if [[ $# -lt 2 ]]; then
    usage >&2
    exit 1
  fi

  environment="$1"
  branch="$2"
}

parse_args "$@"

if [[ ! "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  ns_print_error "Invalid branch name: $branch"
  exit 1
fi

case "$environment" in
  dev)
    compose_files=("docker-compose.yml" "docker-compose.dev.yml")
    ;;
  prod)
    compose_files=("docker-compose.yml")
    ;;
  *)
    ns_print_error "Unknown environment: $environment"
    exit 1
    ;;
esac

env_file="${ENV_FILE:-$ROOT_DIR/.env}"

if [[ -z "${ENV_FILE:-}" ]]; then
  candidate="$ROOT_DIR/deploy/env/.env.$environment"
  if [[ -f "$candidate" ]]; then
    env_file="$candidate"
  elif [[ -f "$ROOT_DIR/.env" ]]; then
    env_file="$ROOT_DIR/.env"
  else
    env_file="$candidate"
  fi
fi

ns_print_header "Ensuring prerequisites"
ns_ensure_prereqs true true false true false false || true

if ! ns_have_cmd docker; then
  ns_print_error "Docker is required but not installed."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  ns_print_error "Docker daemon is not reachable. Start Docker and retry."
  exit 1
fi
if ! ns_compose_available; then
  ns_print_error "Docker Compose is not available (need either 'docker compose' or 'docker-compose')."
  exit 1
fi
if ! ns_have_cmd git; then
  ns_print_error "git is required but not installed."
  exit 1
fi

ns_print_header "Updating code"
git fetch origin "$branch"
git checkout "$branch"
git pull --ff-only origin "$branch"

ns_print_header "Ensuring configuration"
ns_ensure_env_file "$env_file" "$ROOT_DIR"

ns_print_header "Preparing runtime directories"
ns_ensure_runtime_dirs "$ROOT_DIR"
ns_seed_gateway_config_files "$ROOT_DIR"

perms="$(ns_stat_perms "$env_file")"
if [[ -n "$perms" && "$perms" -gt 600 ]]; then
  ns_print_error "Insecure permissions on $env_file (expected 600 or tighter)."
  exit 1
fi

ns_print_header "Running preflight checks"
if [[ -x "$ROOT_DIR/deploy/scripts/preflight-check.sh" ]]; then
  "$ROOT_DIR/deploy/scripts/preflight-check.sh" --mode deploy
else
  ns_print_warn "Preflight checker not executable: deploy/scripts/preflight-check.sh"
fi

compose_args=()
for compose_file in "${compose_files[@]}"; do
  compose_args+=("-f" "$compose_file")
done

ns_compose --env-file "$env_file" "${compose_args[@]}" up -d --build
