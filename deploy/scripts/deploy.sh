#!/usr/bin/env bash
set -euo pipefail
umask 077

# Maintainer note:
# Keep cross-script logic in deploy/scripts/_common.sh (prereqs, env files, prompts,
# validation helpers). Avoid copy/paste changes across scripts.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

NS_AUTO_YES="false"
ENV_FILE=""
SELECTED_COMPONENTS=()
COMPONENTS_SET="false"
EXPLICIT_COMPONENTS_SET="false"
TOPOLOGY_FILE=""
TOPOLOGY_HOST=""

usage() {
  cat <<'EOF'
Usage: deploy/scripts/deploy.sh [--yes] [--env-file PATH] [--component NAME] [--components LIST]
                                [--topology-host NAME] [--topology-file PATH]
                                <environment> <branch>

Suggested order (typical):
  1) ./deploy/scripts/install-host-deps.sh
  2) ./deploy/scripts/import-env.sh   (or: cp .env.example .env)
  3) ./deploy/scripts/preflight-check.sh --mode deploy
  4) ./deploy/scripts/deploy.sh prod main
  5) ./deploy/scripts/verify-gateway.sh

Arguments:
  environment: prod
  branch: git branch to deploy (e.g., dev or main)

Options:
  --yes            Non-interactive (assume "yes" for install prompts)
  --env-file PATH  Env file to use (default: deploy/env/.env.<environment> if present, else ./.env)
  --component NAME Deploy a single component (repeatable)
  --components LIST
                   Deploy a comma-separated set of components
  --topology-host NAME
                   Materialize env and default components from a tracked topology host profile
  --topology-file PATH
                   Topology JSON file (default: deploy/topology/production.json when --topology-host is set)

Components:
  deployment-control, gateway, cloudflared, vllm, vllm-strong, vllm-fast, vllm-embeddings, vllm-meltdown, etcd,
  images, invokeai, sdxl-turbo, lighton-ocr, personaplex, followyourcanvas, ltx-video, hunyuan-video, ace-step,
  heartmula, lifecycle-manager, mediamtx, tts, luxtts, qwen3-tts, telegram-bot, nginx, mlx

Special component groups:
  core             gateway + vllm + etcd
  all              every available component compose file

Examples:
  ./deploy/scripts/deploy.sh prod main
  ./deploy/scripts/deploy.sh --topology-host ai2 prod main
  ./deploy/scripts/deploy.sh --components images prod main
  ./deploy/scripts/deploy.sh --component gateway --component etcd prod main
EOF
}

is_valid_component() {
  case "$1" in
    deployment-control|gateway|cloudflared|vllm|vllm-strong|vllm-fast|vllm-embeddings|vllm-meltdown|etcd|images|invokeai|sdxl-turbo|lighton-ocr|personaplex|followyourcanvas|ltx-video|hunyuan-video|ace-step|heartmula|lifecycle-manager|mediamtx|tts|luxtts|qwen3-tts|telegram-bot|nginx|mlx|core|all)
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
        append_component_unique vllm
        append_component_unique etcd
        ;;
      all)
        append_component_unique deployment-control
        append_component_unique gateway
        append_component_unique cloudflared
        append_component_unique vllm
        append_component_unique mlx
        append_component_unique etcd
        append_component_unique images
        append_component_unique invokeai
        append_component_unique sdxl-turbo
        append_component_unique lighton-ocr
        append_component_unique personaplex
        append_component_unique followyourcanvas
        append_component_unique ltx-video
        append_component_unique hunyuan-video
        append_component_unique ace-step
        append_component_unique heartmula
        append_component_unique lifecycle-manager
        append_component_unique mediamtx
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
    deployment-control) echo "docker-compose.deployment-control.yml" ;;
    gateway) echo "docker-compose.gateway.yml" ;;
    cloudflared) echo "docker-compose.gateway.yml" ;;
    vllm) echo "docker-compose.vllm.yml" ;;
    vllm-strong) echo "docker-compose.vllm-strong.yml" ;;
    vllm-fast) echo "docker-compose.vllm-fast.yml" ;;
    vllm-embeddings) echo "docker-compose.vllm-embeddings.yml" ;;
    vllm-meltdown) echo "docker-compose.vllm-meltdown.yml" ;;
    etcd) echo "docker-compose.etcd.yml" ;;
    images) echo "docker-compose.images.yml" ;;
    invokeai) echo "docker-compose.invokeai.yml" ;;
    sdxl-turbo) echo "docker-compose.sdxl-turbo.yml" ;;
    lighton-ocr) echo "docker-compose.lighton-ocr.yml" ;;
    personaplex) echo "docker-compose.personaplex.yml" ;;
    followyourcanvas) echo "docker-compose.followyourcanvas.yml" ;;
    ltx-video) echo "docker-compose.ltx-video.yml" ;;
    hunyuan-video) echo "docker-compose.hunyuan-video.yml" ;;
    ace-step) echo "docker-compose.ace-step.yml" ;;
    heartmula) echo "docker-compose.heartmula.yml" ;;
    lifecycle-manager) echo "docker-compose.lifecycle-manager.yml" ;;
    mediamtx) echo "docker-compose.mediamtx.yml" ;;
    tts) echo "docker-compose.tts.yml" ;;
    luxtts) echo "docker-compose.luxtts.yml" ;;
    qwen3-tts) echo "docker-compose.qwen3-tts.yml" ;;
    telegram-bot) echo "docker-compose.telegram-bot.yml" ;;
    nginx) echo "docker-compose.nginx.yml" ;;
    mlx) echo "docker-compose.mlx.yml" ;;
    *) return 1 ;;
  esac
}

component_extra_compose_file() {
  case "$1" in
    cloudflared) echo "docker-compose.cloudflared.yml" ;;
    *) echo "" ;;
  esac
}

compose_files_for_component() {
  local component="$1"
  local base_file extra_file
  base_file="$(component_base_compose_file "$component")" || return 1
  printf '%s\n' "$base_file"
  extra_file="$(component_extra_compose_file "$component")"
  if [[ -n "$extra_file" && -f "$ROOT_DIR/$extra_file" ]]; then
    printf '%s\n' "$extra_file"
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
        EXPLICIT_COMPONENTS_SET="true"
        add_component_selection "${2:-}"
        shift 2
        ;;
      --components)
        EXPLICIT_COMPONENTS_SET="true"
        add_component_selection "${2:-}"
        shift 2
        ;;
      --topology-host)
        TOPOLOGY_HOST="${2:-}"
        shift 2
        ;;
      --topology-file)
        TOPOLOGY_FILE="${2:-}"
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

resolve_topology_file() {
  if [[ -n "${TOPOLOGY_FILE:-}" ]]; then
    echo "$TOPOLOGY_FILE"
    return 0
  fi
  echo "$ROOT_DIR/deploy/topology/production.json"
}

parse_args "$@"

if [[ ! "$branch" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
  ns_print_error "Invalid branch name: $branch"
  exit 1
fi

case "$environment" in
  prod)
    ;;
  *)
    ns_print_error "Unsupported environment: $environment (only 'prod' is allowed)"
    exit 1
    ;;
esac

if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  topology_file="$(resolve_topology_file)"
  if [[ ! -f "$topology_file" ]]; then
    ns_print_error "Topology file not found: $topology_file"
    exit 1
  fi
  topology_python="$(ns_pick_python || true)"
  if [[ -z "${topology_python:-}" ]]; then
    ns_print_error "python3/python is required to consume topology manifests."
    exit 1
  fi
  if [[ "$COMPONENTS_SET" != "true" ]]; then
    while IFS= read -r topology_component; do
      [[ -n "${topology_component:-}" ]] || continue
      append_component_unique "$topology_component"
    done < <("$topology_python" "$ROOT_DIR/deploy/scripts/topology_tool.py" components --topology-file "$topology_file" --host "$TOPOLOGY_HOST")
    COMPONENTS_SET="true"
  fi
fi

if [[ "$COMPONENTS_SET" != "true" ]]; then
  SELECTED_COMPONENTS=("gateway" "vllm" "etcd")
fi

compose_files=()
append_compose_file_unique() {
  local candidate="$1"
  local existing
  for existing in "${compose_files[@]:-}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return 0
    fi
  done
  compose_files+=("$candidate")
}

ordered_components=(deployment-control gateway cloudflared vllm vllm-strong vllm-fast vllm-embeddings vllm-meltdown mlx etcd lifecycle-manager images invokeai sdxl-turbo lighton-ocr personaplex followyourcanvas ltx-video hunyuan-video ace-step heartmula mediamtx tts luxtts qwen3-tts telegram-bot nginx)
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
      append_compose_file_unique "$compose_file"
    done < <(compose_files_for_component "$component")
  fi
done

if [[ ${#compose_files[@]} -eq 0 ]]; then
  ns_print_error "No compose files selected for deployment."
  exit 1
fi

component_selected() {
  local wanted="$1"
  local selected
  for selected in "${SELECTED_COMPONENTS[@]:-}"; do
    if [[ "$selected" == "$wanted" ]]; then
      return 0
    fi
  done
  return 1
}

gateway_cloudflared_overlay="false"
if [[ "${TOPOLOGY_HOST:-}" == "ai2" ]] && component_selected gateway && ! component_selected cloudflared; then
  cloudflared_overlay_file="docker-compose.cloudflared.yml"
  if [[ ! -f "$ROOT_DIR/$cloudflared_overlay_file" ]]; then
    ns_print_error "Gateway on ai2 requires ${cloudflared_overlay_file} so the tunnel-origin network is preserved."
    exit 1
  fi
  append_compose_file_unique "$cloudflared_overlay_file"
  gateway_cloudflared_overlay="true"
fi

prepare_cloudflared_runtime() {
  local env_file="$1"
  local host_runtime_root="$2"
  local tunnel_token token_dir token_path token_tmp

  tunnel_token="$(ns_env_get "$env_file" CLOUDFLARED_TUNNEL_TOKEN "")"
  if [[ -z "$tunnel_token" ]]; then
    ns_print_error "CLOUDFLARED_TUNNEL_TOKEN is required when cloudflared is selected."
    exit 1
  fi

  token_dir="$host_runtime_root/cloudflared"
  token_path="$token_dir/tunnel-token"
  token_tmp="$token_dir/.tunnel-token.$$"
  mkdir -p "$token_dir"
  chmod 700 "$token_dir"
  printf '%s' "$tunnel_token" > "$token_tmp"
  chmod 444 "$token_tmp"
  mv -f "$token_tmp" "$token_path"
  unset tunnel_token token_tmp
  ns_print_ok "Cloudflare Tunnel token materialized under the protected runtime directory."
}

topology_essential_components() {
  [[ "${NEXUS_ENSURE_ESSENTIAL_COMPONENTS:-true}" == "true" ]] || return 0
  case "${TOPOLOGY_HOST:-}" in
    ai2)
      printf '%s\n' etcd lifecycle-manager telegram-bot
      ;;
  esac
}

component_service_name() {
  case "$1" in
    lifecycle-manager) echo "lifecycle-manager" ;;
    telegram-bot) echo "telegram-bot" ;;
    *) echo "$1" ;;
  esac
}

ensure_topology_essential_components() {
  local env_file="$1"
  local essential_components=()
  local component compose_file service_name compose_file_seen existing_compose_file
  while IFS= read -r component; do
    [[ -n "${component:-}" ]] || continue
    essential_components+=("$component")
  done < <(topology_essential_components)

  [[ ${#essential_components[@]} -gt 0 ]] || return 0

  local essential_compose_files=()
  local essential_services=()
  essential_compose_files+=("docker-compose.gateway.yml")
  for component in "${essential_components[@]}"; do
    while IFS= read -r compose_file; do
      [[ -n "${compose_file:-}" ]] || continue
      compose_file_seen="false"
      for existing_compose_file in "${essential_compose_files[@]}"; do
        if [[ "$existing_compose_file" == "$compose_file" ]]; then
          compose_file_seen="true"
          break
        fi
      done
      if [[ "$compose_file_seen" != "true" ]]; then
        essential_compose_files+=("$compose_file")
      fi
    done < <(compose_files_for_component "$component")
    service_name="$(component_service_name "$component")"
    essential_services+=("$service_name")
  done

  local essential_compose_args=()
  for compose_file in "${essential_compose_files[@]}"; do
    essential_compose_args+=("-f" "$compose_file")
  done

  ns_print_header "Ensuring essential topology containers"
  GATEWAY_ENV_FILE="${GATEWAY_ENV_FILE:-}" NEXUS_RUNTIME_ROOT="${NEXUS_RUNTIME_ROOT:-}" ns_compose --env-file "$env_file" "${essential_compose_args[@]}" up -d --build --no-recreate "${essential_services[@]}"

  if [[ -f "$ROOT_DIR/deploy/scripts/check-essential-containers.sh" ]]; then
    /bin/bash "$ROOT_DIR/deploy/scripts/check-essential-containers.sh" --wait "${NEXUS_ESSENTIAL_WAIT_SECONDS:-90}"
  fi
}

env_file="${ENV_FILE:-$ROOT_DIR/.env}"

if [[ -z "${ENV_FILE:-}" ]]; then
  if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
    env_file="$ROOT_DIR/deploy/env/.env.$environment.$TOPOLOGY_HOST"
  else
    candidate="$ROOT_DIR/deploy/env/.env.$environment"
    if [[ -f "$candidate" ]]; then
      env_file="$candidate"
    elif [[ -f "$ROOT_DIR/.env" ]]; then
      env_file="$ROOT_DIR/.env"
    else
      env_file="$candidate"
    fi
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
if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  topology_file="$(resolve_topology_file)"
  "$ROOT_DIR/deploy/scripts/render-topology-env.sh" \
    --topology-file "$topology_file" \
    --topology-host "$TOPOLOGY_HOST" \
    --environment "$environment" \
    --env-file "$env_file"
fi
ns_ensure_env_file "$env_file" "$ROOT_DIR"
ns_prepare_sops_env_overlays "$ROOT_DIR" "$environment" "$env_file" "${TOPOLOGY_HOST:-}"
ns_apply_env_overlay_file "$env_file" "$(ns_sops_generated_common_overlay "$env_file")"
ns_apply_env_overlay_file "$env_file" "$(ns_sops_generated_specific_overlay "$env_file")"
ns_apply_env_overlay_file "$env_file" "${env_file}.local"

if component_selected gateway || component_selected cloudflared || component_selected lifecycle-manager || component_selected nginx; then
  missing_extra_host_keys=()
  invalid_extra_host_keys=()
  while IFS= read -r key; do
    [[ -n "${key:-}" ]] || continue
    value="$(ns_env_get "$env_file" "$key" "")"
    if [[ -z "${value:-}" ]]; then
      missing_extra_host_keys+=("$key")
    elif ! ns_is_valid_ipv4 "$value"; then
      invalid_extra_host_keys+=("$key")
    fi
  done < <(ns_gateway_extra_host_env_keys)
  if [[ ${#missing_extra_host_keys[@]} -gt 0 || ${#invalid_extra_host_keys[@]} -gt 0 ]]; then
    if [[ ${#missing_extra_host_keys[@]} -gt 0 ]]; then
      ns_print_error "Selected compose files require these env vars for extra_hosts: ${missing_extra_host_keys[*]}"
    fi
    if [[ ${#invalid_extra_host_keys[@]} -gt 0 ]]; then
      ns_print_error "Selected compose files have invalid IPv4 values for: ${invalid_extra_host_keys[*]}"
    fi
    ns_print_error "Add the required host IPs to ${env_file}, its .local overlay, or its generated SOPS overlay before deploying."
    exit 1
  fi
fi

bind_env_sync_mode="preserve"
if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  bind_env_sync_mode="refresh"
fi
ns_ensure_project_env_bind_source "$ROOT_DIR" "$env_file" "$bind_env_sync_mode"
export GATEWAY_ENV_FILE
GATEWAY_ENV_FILE="$(ns_resolve_docker_env_file "$ROOT_DIR/.env")"
ns_print_ok "Gateway env bind source: ${GATEWAY_ENV_FILE}"
host_runtime_root="$(ns_runtime_root_from_env "$ROOT_DIR" "$env_file")"
export NEXUS_RUNTIME_ROOT
NEXUS_RUNTIME_ROOT="$(ns_resolve_docker_bind_path "$host_runtime_root")"
ns_print_ok "Runtime bind root: ${NEXUS_RUNTIME_ROOT}"

if component_selected cloudflared; then
  prepare_cloudflared_runtime "$env_file" "$host_runtime_root"
fi

ns_print_header "Preparing runtime directories"
ns_ensure_runtime_dirs "$ROOT_DIR"
gateway_config_sync_mode="preserve"
if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  gateway_config_sync_mode="refresh"
fi
ns_seed_gateway_config_files "$ROOT_DIR" "$gateway_config_sync_mode"
ns_verify_docker_bind_source "$ROOT_DIR"
ns_verify_docker_bind_source "$ROOT_DIR/.env"

perms="$(ns_stat_perms "$env_file")"
if [[ -n "$perms" && "$perms" -gt 600 ]]; then
  ns_print_error "Insecure permissions on $env_file (expected 600 or tighter)."
  exit 1
fi

ns_print_header "Running preflight checks"
if [[ -x "$ROOT_DIR/deploy/scripts/preflight-check.sh" ]]; then
  preflight_args=(--mode deploy --env-file "$env_file")
  if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
    preflight_args+=(--topology-host "$TOPOLOGY_HOST" --topology-file "$topology_file")
  fi
  preflight_components=()
  for component in "${SELECTED_COMPONENTS[@]:-}"; do
    if [[ "$component" == "cloudflared" ]]; then
      component="gateway"
    fi
    duplicate="false"
    for existing in "${preflight_components[@]:-}"; do
      if [[ "$existing" == "$component" ]]; then
        duplicate="true"
        break
      fi
    done
    if [[ "$duplicate" != "true" ]]; then
      preflight_components+=("$component")
    fi
  done
  if [[ ${#preflight_components[@]} -gt 0 ]]; then
    preflight_args+=(--components "$(IFS=,; echo "${preflight_components[*]}")")
  fi
  "$ROOT_DIR/deploy/scripts/preflight-check.sh" "${preflight_args[@]}"
else
  ns_print_warn "Preflight checker not executable: deploy/scripts/preflight-check.sh"
fi

compose_args=()
for compose_file in "${compose_files[@]}"; do
  compose_args+=("-f" "$compose_file")
done

ns_print_header "Selected components"
printf 'Requested components: %s\n' "${SELECTED_COMPONENTS[*]}"
printf 'Compose files: %s\n' "${compose_files[*]}"
if [[ "$gateway_cloudflared_overlay" == "true" ]]; then
  ns_print_ok "Gateway Cloudflare overlay enabled; cloudflared remains running unless explicitly selected."
fi

up_args=(up -d --build)
if [[ -n "${TOPOLOGY_HOST:-}" ]]; then
  up_args+=(--force-recreate)
  if [[ "$EXPLICIT_COMPONENTS_SET" == "true" ]]; then
    ns_print_warn "Skipping --remove-orphans for an explicit component-scoped topology deploy."
  else
    up_args+=(--remove-orphans)
  fi
fi

if [[ "$gateway_cloudflared_overlay" == "true" && "$EXPLICIT_COMPONENTS_SET" == "true" ]]; then
  service_targets=()
  for component in "${SELECTED_COMPONENTS[@]:-}"; do
    service_name="$(component_service_name "$component")"
    [[ -n "${service_name:-}" ]] || continue
    service_targets+=("$service_name")
  done
  GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" ns_compose --env-file "$env_file" "${compose_args[@]}" "${up_args[@]}" "${service_targets[@]}"
else
  GATEWAY_ENV_FILE="$GATEWAY_ENV_FILE" NEXUS_RUNTIME_ROOT="$NEXUS_RUNTIME_ROOT" ns_compose --env-file "$env_file" "${compose_args[@]}" "${up_args[@]}"
fi
ensure_topology_essential_components "$env_file"