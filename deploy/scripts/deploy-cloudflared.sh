#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT_DIR"

ENV_FILE=""
BRANCH="main"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/deploy-cloudflared.sh [--env-file PATH] [--branch BRANCH]

Compatibility wrapper for the canonical deployment engine. Routine production
operations should submit the cloudflared component through Deployment Control:

  ./deploy/scripts/request-deploy.sh \
    --host ai2 \
    --component cloudflared \
    --reason "Deploy Cloudflare Tunnel connector"

This wrapper remains available for controller bootstrap or break-glass recovery.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$BRANCH" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  echo "ERROR: Invalid branch name: $BRANCH" >&2
  exit 2
fi

args=(
  --components cloudflared
)
if [[ -n "$ENV_FILE" ]]; then
  args+=(--env-file "$ENV_FILE")
fi

exec "$ROOT_DIR/deploy/scripts/deploy.sh" "${args[@]}" prod "$BRANCH"
