#!/usr/bin/env bash
set -euo pipefail

CONTROLLER_HOST="${NEXUS_DEPLOY_CONTROLLER_SSH:-copyfail}"
REMOTE_REPO_DIR="${NEXUS_DEPLOY_CONTROLLER_REPO_DIR:-/home/ai/ai/nexus}"

if [[ $# -eq 0 ]]; then
  cat <<'EOF'
Usage: deploy/scripts/request-deploy.sh --host HOST --component NAME [options]

Submits a serialized deployment through the copyfail Deployment Control API.
Arguments are forwarded to deploy/scripts/deployment-control-client.py on the
controller. The API token remains on copyfail.
EOF
  exit 2
fi

printf -v quoted_args '%q ' "$@"
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$CONTROLLER_HOST" \
  "cd $(printf '%q' "$REMOTE_REPO_DIR") && python3 deploy/scripts/deployment-control-client.py ${quoted_args}"
