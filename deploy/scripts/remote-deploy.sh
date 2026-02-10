#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <environment> <branch> <host>" >&2
  echo "  environment: dev | prod" >&2
  echo "  branch: git branch to deploy (e.g., dev or main)" >&2
  echo "  host: user@hostname (SSH target)" >&2
  exit 1
fi

environment="$1"
branch="$2"
host="$3"

case "$environment" in
  dev|prod) ;;
  *)
    echo "Unknown environment: $environment" >&2
    exit 1
    ;;
esac

if [[ ! "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  echo "Invalid branch name: $branch" >&2
  exit 1
fi

if [[ ! "$host" =~ ^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$ ]]; then
  echo "Invalid host format: $host (expected user@hostname)" >&2
  exit 1
fi

ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$host" \
  "cd /opt/nexus && ./deploy/scripts/deploy.sh $(printf '%q' "$environment") $(printf '%q' "$branch")"
