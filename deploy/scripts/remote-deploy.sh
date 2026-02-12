#!/usr/bin/env bash
set -euo pipefail
umask 077

# Maintainer note:
# Keep cross-script logic in deploy/scripts/_common.sh (prereqs, prompts, validation).
# Avoid duplicating helpers in individual scripts.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/remote-deploy.sh [--yes] <environment> <branch> <host>

Suggested order (typical):
  1) On the remote host, ensure /opt/nexus exists and is writable by the deploy user
     - Standard deploy user: ai
     - Standard ownership:
       - macOS:  ai:staff
       - Linux:  ai:ai
  2) On the remote host, clone Nexus into /opt/nexus
  3) On the remote host: ./deploy/scripts/install-host-deps.sh
  4) On the remote host: ./deploy/scripts/import-env.sh   (or: cp .env.example .env)
  5) On the remote host: ./deploy/scripts/preflight-check.sh --mode deploy
  6) From your machine: ./deploy/scripts/remote-deploy.sh dev main ai@host

Arguments:
  environment: dev | prod
  branch: git branch to deploy (e.g., dev or main)
  host: user@hostname (SSH target)

Options:
  --yes   Non-interactive mode (assume "yes" for install prompts)
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
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

  if [[ $# -lt 3 ]]; then
    usage >&2
    exit 1
  fi

  environment="$1"
  branch="$2"
  host="$3"
}

parse_args "$@"

ssh_user="${host%@*}"
if [[ "$ssh_user" != "ai" ]]; then
  ns_print_warn "Standard deploy user is 'ai' (you passed '$ssh_user'). Continuing anyway."
fi

case "$environment" in
  dev|prod) ;;
  *)
    ns_print_error "Unknown environment: $environment"
    exit 1
    ;;
esac

if [[ ! "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  ns_print_error "Invalid branch name: $branch"
  exit 1
fi

if [[ ! "$host" =~ ^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$ ]]; then
  ns_print_error "Invalid host format: $host (expected user@hostname)"
  exit 1
fi

ns_print_header "Ensuring prerequisites"
ns_ensure_prereqs false false false false false true || true

if ! ns_have_cmd ssh; then
  ns_print_error "ssh is required but not installed."
  exit 1
fi

ssh_opts=("-o" "StrictHostKeyChecking=accept-new")
if [[ "$NS_AUTO_YES" == "true" ]]; then
  ssh_opts+=("-o" "BatchMode=yes")
else
  ssh_opts+=("-o" "BatchMode=no")
fi

remote_cmd=$(cat <<'EOS'
set -euo pipefail
repo_dir="/opt/nexus"
desired_user="ai"
desired_group="ai"
if [[ "$(uname -s 2>/dev/null || echo unknown)" == "Darwin" ]]; then
  desired_group="staff"
fi

if [[ ! -d "$repo_dir" ]]; then
  echo "ERROR: ${repo_dir} not found on remote host." >&2
  echo "Standard location is ${repo_dir} (owned by ${desired_user}:${desired_group})." >&2
  echo "Create it with:" >&2
  echo "  sudo mkdir -p ${repo_dir}" >&2
  echo "  sudo chown -R ${desired_user}:${desired_group} ${repo_dir}" >&2
  echo "Then clone Nexus into it as '${desired_user}':" >&2
  echo "  git clone <repo-url> ${repo_dir}" >&2
  exit 1
fi

if [[ ! -w "$repo_dir" ]]; then
  echo "ERROR: ${repo_dir} is not writable by $(whoami)." >&2
  echo "Fix ownership/permissions (expected ${desired_user}:${desired_group}):" >&2
  echo "  sudo chown -R ${desired_user}:${desired_group} ${repo_dir}" >&2
  exit 1
fi

cd "$repo_dir"
env_file="${repo_dir}/.env"
candidate="${repo_dir}/deploy/env/.env.$1"
if [[ -f "$candidate" ]]; then
  env_file="$candidate"
fi
./deploy/scripts/preflight-check.sh --mode deploy --env-file "$env_file" || true
./deploy/scripts/deploy.sh "$@"
EOS
)

ssh "${ssh_opts[@]}" "$host" bash -lc "${remote_cmd}" -- "$environment" "$branch"
