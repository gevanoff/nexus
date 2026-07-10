#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

MODEL=""
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/purge-mlx-model-cache.sh --model ORG/REPO [--dry-run]

Erase one MLX Hugging Face cache so a subsequent prefetch downloads it again.
Only validated models--ORG--REPO paths below configured Hugging Face cache roots
are eligible for removal.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_die "Unknown argument: $1"
      ;;
  esac
done

if [[ ! "$MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  ns_die "Model must be a Hugging Face repository id in ORG/REPO form"
fi

if [[ -z "${MLX_NATIVE_ROOT:-}" ]]; then
  if [[ ! -e /var/lib/mlx && -d /ai-data/var/lib/mlx ]]; then
    MLX_NATIVE_ROOT="/ai-data/var/lib/mlx"
  else
    MLX_NATIVE_ROOT="/var/lib/mlx"
  fi
fi
MLX_ENV_FILE="${MLX_ENV_FILE:-${MLX_NATIVE_ROOT}/mlx.env}"

declare -a cache_roots=()
add_cache_root() {
  local root="${1:-}"
  local existing
  [[ -n "$root" && "$root" == /* && "$root" != "/" && -d "$root" ]] || return 0
  root="$(cd "$root" && pwd -P)"
  for existing in "${cache_roots[@]:-}"; do
    [[ "$existing" == "$root" ]] && return 0
  done
  cache_roots+=("$root")
}

add_cache_root "${HF_HOME:-$(ns_env_get "$MLX_ENV_FILE" HF_HOME "")}"
add_cache_root "${HF_HUB_CACHE:-$(ns_env_get "$MLX_ENV_FILE" HF_HUB_CACHE "")}"
add_cache_root "${HUGGINGFACE_HUB_CACHE:-$(ns_env_get "$MLX_ENV_FILE" HUGGINGFACE_HUB_CACHE "")}"
add_cache_root "/ai-data/huggingface"
add_cache_root "/Volumes/ai_data/huggingface"
add_cache_root "/var/lib/huggingface"

if [[ ${#cache_roots[@]} -eq 0 ]]; then
  ns_die "No Hugging Face cache root is available"
fi

repo_name="models--${MODEL//\//--}"
removed_count=0

if [[ "$DRY_RUN" != "true" ]]; then
  prefetch_pids="$(ps -axo pid=,user=,command= | awk -v model="$MODEL" '
    $2 == "mlx" && index($0, "mlx-prefetch-models") && index($0, "--model " model) { print $1 }
  ')"
  if [[ -n "$prefetch_pids" ]]; then
    sudo -n kill $prefetch_pids 2>/dev/null || true
  fi
fi

for cache_root in "${cache_roots[@]}"; do
  for relative in "$repo_name" "hub/$repo_name" ".locks/$repo_name" "hub/.locks/$repo_name"; do
    candidate="${cache_root}/${relative}"
    case "$candidate" in
      "${cache_root}/${repo_name}"|"${cache_root}/hub/${repo_name}"|"${cache_root}/.locks/${repo_name}"|"${cache_root}/hub/.locks/${repo_name}") ;;
      *) ns_die "Refusing unexpected cache path: $candidate" ;;
    esac
    [[ -e "$candidate" ]] || continue
    if [[ "$DRY_RUN" == "true" ]]; then
      echo "would_remove_path=$candidate"
    else
      sudo -n rm -rf -- "$candidate"
      echo "removed_path=$candidate"
    fi
    removed_count=$((removed_count + 1))
  done
done

echo "model=$MODEL"
echo "removed_count=$removed_count"
echo "dry_run=$DRY_RUN"
