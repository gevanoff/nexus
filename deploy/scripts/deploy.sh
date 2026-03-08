#!/usr/bin/env bash
set -euo pipefail
umask 077

# Maintainer note:
# Keep cross-script logic in deploy/scripts/_common.sh (prereqs, env files, prompts,
# validation helpers). Avoid copy/paste changes across scripts.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"
ENV_FILE=""
SELECTED_COMPONENTS=()
COMPONENTS_SET="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/deploy.sh [--yes] [--env-file PATH] [--component NAME] [--components LIST] <environment> <branch>

Suggested order (typical):
  1) ./deploy/scripts/install-host-deps.sh
  2) ./deploy/scripts/import-env.sh   (or: cp .env.example .env)
  3) ./deploy/scripts/preflight-check.sh --mode deploy
  4) ./deploy/scripts/deploy.sh dev main   (or prod)
  5) ./deploy/scripts/verify-gateway.sh

Arguments:
  environment: dev | prod
  branch: git branch to deploy (e.g., dev or main)

Options:
  --yes            Non-interactive (assume "yes" for install prompts)
  --env-file PATH  Env file to use (default: deploy/env/.env.<environment> if present, else ./.env)
  --component NAME Deploy a single component (repeatable)
  --components LIST
                   Deploy a comma-separated set of components

Components:
  gateway, ollama, ollama-linux-nvidia, etcd, images, invokeai, sdxl-turbo,
  lighton-ocr, personaplex, followyourcanvas, skyreels-v2, heartmula,
  tts, luxtts, qwen3-tts, telegram-bot, nginx, mlx

Special component groups:
  core             gateway + ollama + etcd
  all              every available component compose file

Examples:
  ./deploy/scripts/deploy.sh prod main
  ./deploy/scripts/deploy.sh --components images prod main
  ./deploy/scripts/deploy.sh --component gateway --component etcd prod main
EOF
}

is_valid_component() {
  case "$1" in
    gateway|ollama|ollama-linux-nvidia|etcd|images|invokeai|sdxl-turbo|lighton-ocr|personaplex|followyourcanvas|skyreels-v2|heartmula|tts|luxtts|qwen3-tts|telegram-bot|nginx|mlx|core|all)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

append_component_unique() {
  local component="$1"
  local existing
  for existing in "${SELECTED_COMPONENTS[@]:-}"; do
    if [[ "$existing" == "$component" ]]; then
      return 0
    fi
  done
  SELECTED_COMPONENTS+=("$component")
}

add_component_selection() {
  local raw="$1"
  local item normalized
  IFS=',' read -r -a items <<< "$raw"
  for item in "${items[@]}"; do
    normalized="$(echo "$item" | tr -d '[:space:]')"
    [[ -n "$normalized" ]] || continue
    if ! is_valid_component "$normalized"; then
      ns_print_error "Unknown component: $normalized"
      usage
      exit 2
    fi
    COMPONENTS_SET="true"
    case "$normalized" in
      core)
        append_component_unique gateway
        append_component_unique ollama
        append_component_unique etcd
        ;;
      all)
        append_component_unique gateway
        append_component_unique ollama
        append_component_unique ollama-linux-nvidia
        append_component_unique etcd
        append_component_unique images
        append_component_unique invokeai
        append_component_unique sdxl-turbo
        append_component_unique lighton-ocr
        append_component_unique personaplex
        append_component_unique followyourcanvas
        append_component_unique skyreels-v2
        append_component_unique heartmula
        append_component_unique tts
        append_component_unique luxtts
        append_component_unique qwen3-tts
        append_component_unique telegram-bot
        append_component_unique nginx
        append_component_unique mlx
        ;;
      *)
        append_component_unique "$normalized"
        ;;
    esac
  done
}

component_base_compose_file() {
  case "$1" in
    gateway) echo "docker-compose.gateway.yml" ;;
    ollama) echo "docker-compose.ollama.yml" ;;
    ollama-linux-nvidia) echo "docker-compose.ollama.yml" ;;
    etcd) echo "docker-compose.etcd.yml" ;;
    images) echo "docker-compose.images.yml" ;;
    invokeai) echo "docker-compose.invokeai.yml" ;;
    sdxl-turbo) echo "docker-compose.sdxl-turbo.yml" ;;
    lighton-ocr) echo "docker-compose.lighton-ocr.yml" ;;
    personaplex) echo "docker-compose.personaplex.yml" ;;
    followyourcanvas) echo "docker-compose.followyourcanvas.yml" ;;
    skyreels-v2) echo "docker-compose.skyreels-v2.yml" ;;
    heartmula) echo "docker-compose.heartmula.yml" ;;
    tts) echo "docker-compose.tts.yml" ;;
    luxtts) echo "docker-compose.luxtts.yml" ;;
    qwen3-tts) echo "docker-compose.qwen3-tts.yml" ;;
    telegram-bot) echo "docker-compose.telegram-bot.yml" ;;
    nginx) echo "docker-compose.nginx.yml" ;;
    mlx) echo "docker-compose.mlx.yml" ;;
    *) return 1 ;;
  esac
}

component_dev_compose_file() {
  case "$1" in
    gateway) echo "docker-compose.gateway.dev.yml" ;;
    ollama) echo "docker-compose.ollama.dev.yml" ;;
    etcd) echo "docker-compose.etcd.dev.yml" ;;
    images) echo "docker-compose.images.dev.yml" ;;
    tts) echo "docker-compose.tts.dev.yml" ;;
    *) echo "" ;;
  esac
}

component_extra_compose_file() {
  case "$1" in
    ollama-linux-nvidia) echo "docker-compose.ollama.linux-nvidia.yml" ;;
    *) echo "" ;;
  esac
}

compose_files_for_component() {
  local component="$1"
  local base_file dev_file extra_file
  base_file="$(component_base_compose_file "$component")" || return 1
  printf '%s\n' "$base_file"
  extra_file="$(component_extra_compose_file "$component")"
  if [[ -n "$extra_file" && -f "$ROOT_DIR/$extra_file" ]]; then
    printf '%s\n' "$extra_file"
  fi
  if [[ "$environment" == "dev" ]]; then
    dev_file="$(component_dev_compose_file "$component")"
    if [[ -n "$dev_file" && -f "$ROOT_DIR/$dev_file" ]]; then
      printf '%s\n' "$dev_file"
    fi
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes)
        NS_AUTO_YES="true"
        shift
        ;;
      --env-file)
        ENV_FILE="${2:-}"
        shift 2
        ;;
      --component)
        add_component_selection "${2:-}"
        shift 2
        ;;
      --components)
        add_component_selection "${2:-}"
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

  if [[ $# -lt 2 ]]; then
    usage >&2
    exit 1
  fi

  environment="$1"
  branch="$2"
}

parse_args "$@"

if [[ ! "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  ns_print_error "Invalid branch name: $branch"
  exit 1
fi

case "$environment" in
  dev|prod)
    ;;
  *)
    ns_print_error "Unknown environment: $environment"
    exit 1
    ;;
esac

if [[ "$COMPONENTS_SET" != "true" ]]; then
  SELECTED_COMPONENTS=("gateway")
fi

compose_files=()
ordered_components=(gateway ollama ollama-linux-nvidia etcd images invokeai sdxl-turbo lighton-ocr personaplex followyourcanvas skyreels-v2 heartmula tts luxtts qwen3-tts telegram-bot nginx mlx)
for component in "${ordered_components[@]}"; do
  include_component="false"
  for selected in "${SELECTED_COMPONENTS[@]}"; do
    if [[ "$selected" == "$component" ]]; then
      include_component="true"
      break
    fi
  done
  if [[ "$include_component" == "true" ]]; then
    while IFS= read -r compose_file; do
      [[ -n "$compose_file" ]] || continue
      compose_files+=("$compose_file")
    done < <(compose_files_for_component "$component")
  fi
done

if [[ ${#compose_files[@]} -eq 0 ]]; then
  ns_print_error "No compose files selected for deployment."
  exit 1
fi

env_file="${ENV_FILE:-$ROOT_DIR/.env}"

if [[ -z "${ENV_FILE:-}" ]]; then
  candidate="$ROOT_DIR/deploy/env/.env.$environment"
  if [[ -f "$candidate" ]]; then
    env_file="$candidate"
  elif [[ -f "$ROOT_DIR/.env" ]]; then
    env_file="$ROOT_DIR/.env"
  else
    env_file="$candidate"
  fi
fi

ns_print_header "Ensuring prerequisites"
ns_ensure_prereqs true true false true false false || true

if ! ns_have_cmd docker; then
  ns_print_error "Docker is required but not installed."
  exit 1
fi
if ! ns_ensure_docker_daemon true; then
  ns_print_error "Docker daemon is not reachable. Start Docker and retry."
  exit 1
fi
if ! ns_compose_available; then
  ns_print_error "Docker Compose is not available (need either 'docker compose' or 'docker-compose')."
  exit 1
fi
if ! ns_have_cmd git; then
  ns_print_error "git is required but not installed."
  exit 1
fi

ns_print_header "Updating code"
git fetch origin "$branch"
git checkout "$branch"
git pull --ff-only origin "$branch"

ns_print_header "Ensuring configuration"
ns_ensure_env_file "$env_file" "$ROOT_DIR"
ns_ensure_project_env_bind_source "$ROOT_DIR" "$env_file"

ns_print_header "Preparing runtime directories"
ns_ensure_runtime_dirs "$ROOT_DIR"
ns_seed_gateway_config_files "$ROOT_DIR"
ns_verify_docker_bind_source "$ROOT_DIR"
ns_verify_docker_bind_source "$ROOT_DIR/.env"

perms="$(ns_stat_perms "$env_file")"
if [[ -n "$perms" && "$perms" -gt 600 ]]; then
  ns_print_error "Insecure permissions on $env_file (expected 600 or tighter)."
  exit 1
fi

ns_print_header "Running preflight checks"
if [[ -x "$ROOT_DIR/deploy/scripts/preflight-check.sh" ]]; then
  "$ROOT_DIR/deploy/scripts/preflight-check.sh" --mode deploy --env-file "$env_file"
else
  ns_print_warn "Preflight checker not executable: deploy/scripts/preflight-check.sh"
fi

compose_args=()
for compose_file in "${compose_files[@]}"; do
  compose_args+=("-f" "$compose_file")
done

ns_print_header "Selected components"
printf 'Compose files: %s\n' "${compose_files[*]}"

ns_compose --env-file "$env_file" "${compose_args[@]}" up -d --build
