#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <environment> <branch>" >&2
  echo "  environment: dev | prod" >&2
  echo "  branch: git branch to deploy (e.g., dev or main)" >&2
  exit 1
fi

environment="$1"
branch="$2"

if [[ ! "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  echo "Invalid branch name: $branch" >&2
  exit 1
fi

case "$environment" in
  dev)
    env_file="deploy/env/.env.dev"
    compose_files=("docker-compose.yml" "docker-compose.dev.yml")
    ;;
  prod)
    env_file="deploy/env/.env.prod"
    compose_files=("docker-compose.yml")
    ;;
  *)
    echo "Unknown environment: $environment" >&2
    exit 1
    ;;
esac

if [[ ! -f "$env_file" ]]; then
  echo "Missing env file: $env_file" >&2
  exit 1
fi

if ! stat -c '%a' "$env_file" >/dev/null 2>&1; then
  echo "Unable to read permissions for $env_file" >&2
  exit 1
fi

env_perms=$(stat -c '%a' "$env_file")
if [[ "$env_perms" -gt 600 ]]; then
  echo "Insecure permissions on $env_file (expected 600 or tighter)." >&2
  exit 1
fi

git fetch origin "$branch"
git checkout "$branch"
git pull --ff-only origin "$branch"

compose_args=()
for compose_file in "${compose_files[@]}"; do
  compose_args+=("-f" "$compose_file")
done

docker compose --env-file "$env_file" "${compose_args[@]}" up -d --build
