#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT_DIR"

# Compatibility entrypoint only. Keep deployment behavior in deploy.sh.
TOPOLOGY_HOST=""
ENV_FILE=""
TOPOLOGY_FILE=""
ENVIRONMENT="prod"
BRANCH="main"
BRANCH_SET="false"
AUTO_YES="false"
COMPONENT_ARGS=()
POSITIONAL=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/ops-stack.sh --topology-host HOST [OPTIONS] [prod BRANCH]

Topology-aware convenience wrapper for deploy/scripts/deploy.sh. Production
deployment behavior lives in deploy.sh; this wrapper only supplies the common
prod/main defaults and prevents the retired compose-selection path from being
used accidentally.

Options:
  --topology-host HOST  Required tracked production host (for example ai2 or stackrot)
  --env-file PATH       Forward an explicit env file to deploy.sh
  --topology-file PATH  Forward an alternate topology manifest to deploy.sh
  --component NAME      Deploy one component (repeatable)
  --components LIST     Deploy a comma-separated component list
  --branch BRANCH       Compatibility spelling for the optional BRANCH argument
  --yes                 Forward non-interactive confirmation to deploy.sh

Defaults:
  environment: prod
  branch:      main

Examples:
  ./deploy/scripts/ops-stack.sh --topology-host ai2
  ./deploy/scripts/ops-stack.sh --topology-host stackrot prod main
  ./deploy/scripts/ops-stack.sh --topology-host ai2 --component gateway

The retired --no-pull, --no-build, --no-verify, --external-vllm,
--external-mlx, --with-mlx, and --with-telegram flags are intentionally not
supported. Use a focused restart helper for no-build operations, or select
components through the topology-aware deploy command.
EOF
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == -* ]]; then
    printf 'ERROR: %s requires a value.\n' "$option" >&2
    exit 2
  fi
}

legacy_option_error() {
  local option="$1"
  printf 'ERROR: %s belonged to the retired compose-selection implementation.\n' "$option" >&2
  printf 'Use --topology-host HOST with this wrapper, deploy.sh directly, or a focused restart helper.\n' >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topology-host)
      require_value "$1" "${2:-}"
      TOPOLOGY_HOST="$2"
      shift 2
      ;;
    --env-file)
      require_value "$1" "${2:-}"
      ENV_FILE="$2"
      shift 2
      ;;
    --topology-file)
      require_value "$1" "${2:-}"
      TOPOLOGY_FILE="$2"
      shift 2
      ;;
    --component | --components)
      require_value "$1" "${2:-}"
      COMPONENT_ARGS+=("$1" "$2")
      shift 2
      ;;
    --branch)
      require_value "$1" "${2:-}"
      BRANCH="$2"
      BRANCH_SET="true"
      shift 2
      ;;
    --yes)
      AUTO_YES="true"
      shift
      ;;
    --no-pull | --no-build | --no-verify | --external-vllm | --external-mlx | --with-mlx | --with-telegram)
      legacy_option_error "$1"
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        POSITIONAL+=("$1")
        shift
      done
      ;;
    -*)
      printf 'ERROR: Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$TOPOLOGY_HOST" ]]; then
  printf 'ERROR: --topology-host is required; the legacy host-agnostic core stack is retired.\n' >&2
  usage >&2
  exit 2
fi

if [[ ${#POSITIONAL[@]} -gt 2 ]]; then
  printf 'ERROR: Expected at most environment and branch positional arguments.\n' >&2
  usage >&2
  exit 2
fi
if [[ ${#POSITIONAL[@]} -ge 1 ]]; then
  ENVIRONMENT="${POSITIONAL[0]}"
fi
if [[ ${#POSITIONAL[@]} -eq 2 ]]; then
  if [[ "$BRANCH_SET" == "true" ]]; then
    printf 'ERROR: Specify the branch with either --branch or a positional argument, not both.\n' >&2
    exit 2
  fi
  BRANCH="${POSITIONAL[1]}"
fi

DEPLOY_ARGS=(--topology-host "$TOPOLOGY_HOST")
if [[ -n "$ENV_FILE" ]]; then
  DEPLOY_ARGS+=(--env-file "$ENV_FILE")
fi
if [[ -n "$TOPOLOGY_FILE" ]]; then
  DEPLOY_ARGS+=(--topology-file "$TOPOLOGY_FILE")
fi
if [[ "$AUTO_YES" == "true" ]]; then
  DEPLOY_ARGS+=(--yes)
fi
DEPLOY_ARGS+=("${COMPONENT_ARGS[@]}")

printf 'Delegating topology-aware deployment to deploy/scripts/deploy.sh (%s/%s).\n' "$ENVIRONMENT" "$BRANCH"
exec "$ROOT_DIR/deploy/scripts/deploy.sh" "${DEPLOY_ARGS[@]}" "$ENVIRONMENT" "$BRANCH"
