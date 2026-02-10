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

ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$host" "cd /opt/nexus && ./deploy/scripts/deploy.sh $environment $branch"
