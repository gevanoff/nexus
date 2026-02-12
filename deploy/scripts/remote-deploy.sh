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
  1) On the remote host, clone Nexus into /opt/nexus
  2) On the remote host: ./deploy/scripts/install-host-deps.sh
  3) On the remote host: ./deploy/scripts/import-env.sh   (or: cp .env.example .env)
  4) On the remote host: ./deploy/scripts/preflight-check.sh --mode deploy
  5) From your machine: ./deploy/scripts/remote-deploy.sh dev main user@host

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
if [[ ! -d /opt/nexus ]]; then
  echo "ERROR: /opt/nexus not found on remote host." >&2
  echo "Clone the Nexus repo on the remote host at /opt/nexus, then re-run." >&2
  exit 1
fi
cd /opt/nexus
./deploy/scripts/preflight-check.sh --mode deploy || true
./deploy/scripts/deploy.sh "$@"
EOS
)

ssh "${ssh_opts[@]}" "$host" bash -lc "${remote_cmd}" -- "$environment" "$branch"
