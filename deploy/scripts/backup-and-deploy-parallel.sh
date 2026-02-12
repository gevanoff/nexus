#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

BACKUP_DIR=""
ENV_FILE=""
NO_INTERACTIVE=0
INCLUDE_OLLAMA=0

usage() {
  cat <<'EOF'
Usage: deploy/scripts/backup-and-deploy-parallel.sh [options]

Backs up legacy host data (if present) and deploys Nexus on parallel ports so it
can run side-by-side with an existing ai-infra/gateway deployment.

Options:
  --backup-dir <path>    Backup output directory (default: ./deploy/backups/<timestamp>)
  --env-file <path>      Env file to deploy with (default: ./deploy/env/.env.parallel)
  --include-ollama       Also archive /var/lib/ollama (can be large)
  --yes                  Non-interactive mode (auto-accept confirmations)
  -h, --help             Show help
EOF
}

confirm() {
  local prompt="$1"
  if [[ "$NO_INTERACTIVE" -eq 1 ]]; then
    return 0
  fi
  ns_confirm "$prompt"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

timestamp() {
  if need_cmd date; then
    date +%Y%m%d-%H%M%S
  else
    echo "$(python3 - <<'PY'
import datetime
print(datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S'))
PY
)"
  fi
}

ensure_env_file_parallel() {
  local env_file="$1"

  if [[ ! -f "$env_file" ]]; then
    ns_print_warn "Creating env file: $env_file"
    ns_mkdir_p "$(dirname "$env_file")"
    cp "$ROOT_DIR/.env.example" "$env_file"
    chmod 600 "$env_file" 2>/dev/null || true
  fi

  # Force parallel ports (non-destructive: replace existing keys)
  local edits=(
    "GATEWAY_PORT=18800"
    "OBSERVABILITY_PORT=18801"
    "OLLAMA_PORT=21434"
    "IMAGES_PORT=17860"
    "TTS_PORT=19940"
    "ETCD_PORT=12379"
  )

  for kv in "${edits[@]}"; do
    local key="${kv%%=*}"
    if grep -qE "^${key}=" "$env_file"; then
      if [[ "$(ns_detect_platform)" == "macos" ]]; then
        sed -i '' "s/^${key}=.*/${kv}/" "$env_file"
      else
        sed -i "s/^${key}=.*/${kv}/" "$env_file"
      fi
    else
      printf '\n%s\n' "$kv" >>"$env_file"
    fi
  done

  # Ensure we have a non-placeholder token.
  local token
  token="$(grep -E '^GATEWAY_BEARER_TOKEN=' "$env_file" | head -n 1 | cut -d '=' -f2- || true)"
  if [[ -z "${token:-}" || "$token" == "change-me-in-production" || "$token" == "your-secret-token-here" ]]; then
    token="$(ns_generate_token | tr -d '\r\n')"
    if [[ "$(ns_detect_platform)" == "macos" ]]; then
      sed -i '' "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$token/" "$env_file"
    else
      sed -i "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$token/" "$env_file"
    fi
    ns_print_ok "Generated GATEWAY_BEARER_TOKEN in $env_file"
  fi
}

backup_dir_tar() {
  local src_dir="$1"
  local out_file="$2"

  if [[ ! -d "$src_dir" ]]; then
    ns_print_warn "Skipping backup; not found: $src_dir"
    return 0
  fi

  ns_print_header "Backing up: $src_dir"

  if [[ -r "$src_dir" ]]; then
    tar czf "$out_file" -C "$src_dir" .
  else
    if need_cmd sudo; then
      sudo tar czf "$out_file" -C "$src_dir" .
    else
      ns_print_error "Cannot read $src_dir (needs elevated permissions) and sudo is unavailable."
      return 1
    fi
  fi
  chmod 600 "$out_file" 2>/dev/null || true
  ns_print_ok "Wrote backup: $out_file"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="${2:-}"; shift 2 ;;
    --env-file)
      ENV_FILE="${2:-}"; shift 2 ;;
    --include-ollama)
      INCLUDE_OLLAMA=1; shift ;;
    --yes)
      NO_INTERACTIVE=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      ns_print_error "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "${BACKUP_DIR:-}" ]]; then
  BACKUP_DIR="$ROOT_DIR/deploy/backups/$(timestamp)"
fi
if [[ -z "${ENV_FILE:-}" ]]; then
  ENV_FILE="$ROOT_DIR/deploy/env/.env.parallel"
fi

ns_require_cmd docker
ns_require_cmd curl

if ! docker info >/dev/null 2>&1; then
  ns_die "Docker daemon is not reachable. Start Docker and retry."
fi
if ! ns_compose_available; then
  ns_die "Docker Compose is not available (need either 'docker compose' or 'docker-compose')."
fi

ns_mkdir_p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

# 1) Backups (best-effort)
backup_dir_tar "/var/lib/gateway/data" "$BACKUP_DIR/legacy-gateway-data.tar.gz" || true
if [[ -f "/var/lib/gateway/app/.env" ]]; then
  cp "/var/lib/gateway/app/.env" "$BACKUP_DIR/legacy-gateway.env" || true
  chmod 600 "$BACKUP_DIR/legacy-gateway.env" 2>/dev/null || true
  ns_print_ok "Backed up: /var/lib/gateway/app/.env"
fi

if [[ "$INCLUDE_OLLAMA" -eq 1 ]]; then
  if confirm "Archive /var/lib/ollama? (can be large)"; then
    backup_dir_tar "/var/lib/ollama" "$BACKUP_DIR/legacy-ollama.tar.gz" || true
  fi
fi

# 2) Prepare Nexus runtime + parallel env
ns_print_header "Preparing Nexus (parallel ports)"
ensure_env_file_parallel "$ENV_FILE"
ns_ensure_runtime_dirs "$ROOT_DIR"
ns_seed_gateway_config_files "$ROOT_DIR"

# 3) Deploy
ns_print_header "Deploying Nexus (parallel)"
ns_compose --env-file "$ENV_FILE" up -d --build

# 4) Verify
ns_print_header "Verifying Nexus (parallel)"
GATEWAY_PORT="$(grep -E '^GATEWAY_PORT=' "$ENV_FILE" | head -n 1 | cut -d '=' -f2- || echo 18800)"
TOKEN="$(grep -E '^GATEWAY_BEARER_TOKEN=' "$ENV_FILE" | head -n 1 | cut -d '=' -f2- || true)"

curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null
ns_print_ok "Gateway /health reachable on :${GATEWAY_PORT}"

if [[ -n "${TOKEN:-}" ]]; then
  curl -fsS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${GATEWAY_PORT}/v1/models" >/dev/null || true
fi

ns_print_ok "Parallel deploy complete"
ns_print_ok "Backups written under: $BACKUP_DIR"
ns_print_ok "Parallel env file: $ENV_FILE"
