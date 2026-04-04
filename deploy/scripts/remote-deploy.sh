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
DEPLOY_OPTIONS=()
TOPOLOGY_FILE="${ROOT_DIR}/deploy/topology/production.json"
TOPOLOGY_HOST=""
REMOTE_REPO_DIR=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/remote-deploy.sh [--yes] [--component NAME] [--components LIST]
                                       [--topology-host NAME] [--topology-file PATH]
                                       [--repo-dir PATH]
                                       <environment> <branch> [host]

Suggested order (typical):
  1) On the remote host, ensure the standard repo path exists and is writable by the deploy user
     - Standard deploy user: ai
     - Standard repo path:
       - macOS:  /Users/ai/ai/nexus
       - Linux:  /home/ai/ai/nexus
     - Standard ownership:
       - macOS:  ai:staff
       - Linux:  ai:ai
  2) On the remote host, clone Nexus into the platform-specific standard repo path
  3) On the remote host: ./deploy/scripts/install-host-deps.sh
  4) On the remote host: ./deploy/scripts/import-env.sh   (or: cp .env.example .env)
  5) On the remote host: ./deploy/scripts/preflight-check.sh --mode deploy
  6) From your machine: ./deploy/scripts/remote-deploy.sh dev main ai@host

Arguments:
  environment: dev | prod
  branch: git branch to deploy (e.g., dev or main)
  host: user@hostname (SSH target). Optional when --topology-host is set.

Options:
  --yes   Non-interactive mode (assume "yes" for install prompts)
  --component NAME
          Forward a single component selection to deploy.sh (repeatable)
  --components LIST
          Forward a comma-separated component list to deploy.sh
  --topology-host NAME
          Forward a topology host profile to deploy.sh on the remote host
  --topology-file PATH
          Forward an explicit topology file path to deploy.sh on the remote host
  --repo-dir PATH
          Override the remote Nexus checkout path (default: topology repo_dir or fallback /opt/nexus)
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
        ;;
      --component)
        DEPLOY_OPTIONS+=("--component" "${2:-}")
        shift 2
        ;;
      --components)
        DEPLOY_OPTIONS+=("--components" "${2:-}")
        shift 2
        ;;
      --topology-host)
        TOPOLOGY_HOST="${2:-}"
        DEPLOY_OPTIONS+=("--topology-host" "${2:-}")
        shift 2
        ;;
      --topology-file)
        TOPOLOGY_FILE="${2:-}"
        DEPLOY_OPTIONS+=("--topology-file" "${2:-}")
        shift 2
        ;;
      --repo-dir)
        REMOTE_REPO_DIR="${2:-}"
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

  if [[ $# -lt 2 || $# -gt 3 ]]; then
    usage >&2
    exit 1
  fi

  environment="$1"
  branch="$2"
  host="${3:-}"
}

resolve_topology_target() {
  local python_bin
  python_bin="$(ns_pick_python || true)"
  [[ -n "${python_bin:-}" ]] || ns_die "python3/python is required when --topology-host is used."

  local resolved_host
  resolved_host="$("$python_bin" "$ROOT_DIR/deploy/scripts/topology_tool.py" ssh-target \
    --topology-file "$TOPOLOGY_FILE" \
    --host "$TOPOLOGY_HOST")"
  [[ -n "${resolved_host:-}" ]] || ns_die "Failed to resolve ssh target for topology host ${TOPOLOGY_HOST}."

  local resolved_repo_dir
  resolved_repo_dir="$("$python_bin" "$ROOT_DIR/deploy/scripts/topology_tool.py" repo-dir \
    --topology-file "$TOPOLOGY_FILE" \
    --host "$TOPOLOGY_HOST")"
  [[ -n "${resolved_repo_dir:-}" ]] || ns_die "Failed to resolve repo_dir for topology host ${TOPOLOGY_HOST}."

  if [[ -n "${host:-}" && "$host" != "$resolved_host" ]]; then
    ns_die "Host argument ${host} does not match topology host ${TOPOLOGY_HOST} (${resolved_host}). Omit the host argument or use the topology target."
  fi

  host="$resolved_host"
  if [[ -z "${REMOTE_REPO_DIR:-}" ]]; then
    REMOTE_REPO_DIR="$resolved_repo_dir"
  fi
}

parse_args "$@"

if [[ -z "${host:-}" && -z "${TOPOLOGY_HOST:-}" ]]; then
  usage >&2
  exit 1
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

ns_print_header "Ensuring prerequisites"
if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  ns_ensure_prereqs false false false false true true || true
else
  ns_ensure_prereqs false false false false false true || true
fi

if ! ns_have_cmd ssh; then
  ns_print_error "ssh is required but not installed."
  exit 1
fi

if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  resolve_topology_target
fi

if [[ -z "${REMOTE_REPO_DIR:-}" ]]; then
  REMOTE_REPO_DIR="/opt/nexus"
fi

ssh_user="${host%@*}"
if [[ "$ssh_user" != "ai" ]]; then
  ns_print_warn "Standard deploy user is 'ai' (you passed '$ssh_user'). Continuing anyway."
fi

if [[ ! "$host" =~ ^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$ ]]; then
  ns_print_error "Invalid host format: $host (expected user@hostname)"
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
repo_dir="$3"
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
topology_host="${4:-}"
candidate="${repo_dir}/deploy/env/.env.$1"
if [[ -n "$topology_host" ]]; then
  candidate="${repo_dir}/deploy/env/.env.$1.$topology_host"
fi
if [[ -f "$candidate" ]]; then
  env_file="$candidate"
fi
./deploy/scripts/preflight-check.sh --mode deploy --env-file "$env_file" || true
./deploy/scripts/deploy.sh "${@:5}" "$1" "$2"
EOS
)

ssh "${ssh_opts[@]}" "$host" bash -lc "${remote_cmd}" -- "$environment" "$branch" "$REMOTE_REPO_DIR" "$TOPOLOGY_HOST" "${DEPLOY_OPTIONS[@]}"
