#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR=""
AI_INFRA_DIR=""
NEXUS_DIR="$ROOT_DIR"
SKIP_DEPLOY=0
SKIP_RESTORE=0
NO_INTERACTIVE=0

usage() {
  cat <<USAGE
Usage: $0 [options]

Interactive migration helper from ai-infra to Nexus.

Options:
  --backup-dir <path>    Directory where backup files should be written.
  --ai-infra-dir <path>  Path to existing ai-infra checkout.
  --nexus-dir <path>     Path to Nexus repository (default: current repo root).
  --skip-deploy          Skip 'docker compose up -d'.
  --skip-restore         Skip data/config restore steps.
  --yes                  Non-interactive mode (auto-accept confirmations).
  -h, --help             Show this help text.
USAGE
}

confirm() {
  local prompt="$1"
  local reply
  if [[ "$NO_INTERACTIVE" -eq 1 ]]; then
    return 0
  fi
  read -r -p "$prompt [y/N]: " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_cmd() {
  local cmd="$1"
  if ! need_cmd "$cmd"; then
    echo "Required command missing: $cmd" >&2
    exit 1
  fi
}

require_dir() {
  local path="$1"
  local label="$2"
  if [[ ! -d "$path" ]]; then
    echo "Missing directory for $label: $path" >&2
    exit 1
  fi
}

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    echo "Missing file for $label: $path" >&2
    exit 1
  fi
}

container_id_for() {
  local service="$1"
  docker compose ps -q "$service" | head -n1
}

backup_ai_infra() {
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"

  if [[ -d /var/lib/gateway/data ]]; then
    echo "Backing up gateway data from /var/lib/gateway/data"
    if need_cmd sudo; then
      sudo tar czf "$BACKUP_DIR/gateway-backup.tar.gz" -C /var/lib/gateway/data .
    else
      tar czf "$BACKUP_DIR/gateway-backup.tar.gz" -C /var/lib/gateway/data .
    fi
    chmod 600 "$BACKUP_DIR/gateway-backup.tar.gz"
  else
    echo "Skipping gateway data backup; /var/lib/gateway/data not found."
  fi

  if need_cmd ollama; then
    echo "Exporting installed Ollama models"
    ollama list > "$BACKUP_DIR/ollama-models.txt" || true
    chmod 600 "$BACKUP_DIR/ollama-models.txt" || true
  else
    echo "Skipping model list export; 'ollama' CLI not found."
  fi

  if [[ -d /var/lib/ollama ]]; then
    if confirm "Create full /var/lib/ollama archive? (can be large)"; then
      if need_cmd sudo; then
        sudo tar czf "$BACKUP_DIR/ollama-backup.tar.gz" -C /var/lib/ollama .
      else
        tar czf "$BACKUP_DIR/ollama-backup.tar.gz" -C /var/lib/ollama .
      fi
      chmod 600 "$BACKUP_DIR/ollama-backup.tar.gz"
    fi
  else
    echo "Skipping Ollama data backup; /var/lib/ollama not found."
  fi

  local gateway_env="$AI_INFRA_DIR/services/gateway/env"
  for file in gateway.env model_aliases.json tools_registry.json agent_specs.json; do
    if [[ -f "$gateway_env/$file" ]]; then
      cp "$gateway_env/$file" "$BACKUP_DIR/$file.backup"
      chmod 600 "$BACKUP_DIR/$file.backup"
      echo "Backed up $file"
    fi
  done
}

prepare_nexus() {
  cd "$NEXUS_DIR"
  require_file .env.example "nexus env template"

  if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    echo "Created .env from .env.example"
  fi

  local token
  if grep -q '^GATEWAY_BEARER_TOKEN=' .env; then
    token="$(grep '^GATEWAY_BEARER_TOKEN=' .env | cut -d= -f2-)"
    if [[ -z "$token" ]]; then
      token="$(openssl rand -hex 32)"
      sed -i "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$token/" .env
      echo "Generated secure GATEWAY_BEARER_TOKEN in .env"
    fi
  else
    token="$(openssl rand -hex 32)"
    printf '\nGATEWAY_BEARER_TOKEN=%s\n' "$token" >> .env
    chmod 600 .env
    echo "Added GATEWAY_BEARER_TOKEN to .env"
  fi

  if [[ "$SKIP_DEPLOY" -eq 0 ]]; then
    docker compose up -d
  fi
}

restore_into_nexus() {
  cd "$NEXUS_DIR"
  local gateway_cid ollama_cid
  gateway_cid="$(container_id_for gateway)"
  ollama_cid="$(container_id_for ollama)"

  if [[ -z "$gateway_cid" ]]; then
    echo "Gateway container not found; cannot restore gateway data." >&2
  elif [[ -f "$BACKUP_DIR/gateway-backup.tar.gz" ]]; then
    docker cp "$BACKUP_DIR/gateway-backup.tar.gz" "$gateway_cid:/tmp/gateway-backup.tar.gz"
    docker compose exec -T gateway tar xzf /tmp/gateway-backup.tar.gz -C /data
  fi

  if [[ -n "$gateway_cid" ]]; then
    if [[ -f "$BACKUP_DIR/model_aliases.json.backup" ]]; then
      docker cp "$BACKUP_DIR/model_aliases.json.backup" "$gateway_cid:/data/model_aliases.json"
    fi
    if [[ -f "$BACKUP_DIR/tools_registry.json.backup" ]]; then
      docker cp "$BACKUP_DIR/tools_registry.json.backup" "$gateway_cid:/data/tools_registry.json"
    fi
    if [[ -f "$BACKUP_DIR/agent_specs.json.backup" ]]; then
      docker cp "$BACKUP_DIR/agent_specs.json.backup" "$gateway_cid:/data/agent_specs.json"
    fi
    docker compose restart gateway || true
  fi

  if [[ -n "$ollama_cid" && -f "$BACKUP_DIR/ollama-models.txt" ]]; then
    if confirm "Pull models from ollama-models.txt into the new Ollama container?"; then
      while IFS= read -r model_line; do
        local model
        model="$(awk '{print $1}' <<<"$model_line")"
        [[ -n "$model" && "$model" != "NAME" ]] || continue
        docker compose exec -T ollama ollama pull "$model" || true
      done < "$BACKUP_DIR/ollama-models.txt"
    fi
  fi

  if [[ -n "$ollama_cid" && -f "$BACKUP_DIR/ollama-backup.tar.gz" ]]; then
    if confirm "Restore full ollama-backup.tar.gz into /root/.ollama?"; then
      docker cp "$BACKUP_DIR/ollama-backup.tar.gz" "$ollama_cid:/tmp/ollama-backup.tar.gz"
      docker compose exec -T ollama tar xzf /tmp/ollama-backup.tar.gz -C /root/.ollama
      docker compose restart ollama
    fi
  fi
}

verify_migration() {
  cd "$NEXUS_DIR"
  docker compose ps
  curl -fsS http://localhost:8800/health >/dev/null
  echo "Migration verification: gateway /health is reachable"

  local token
  token="$(grep '^GATEWAY_BEARER_TOKEN=' .env | cut -d= -f2-)"
  if [[ -n "$token" ]]; then
    curl -fsS -H "Authorization: Bearer $token" http://localhost:8800/v1/models >/dev/null || true
  fi
}

stop_legacy_services() {
  if confirm "Stop old ai-infra services now?"; then
    if [[ -x "$AI_INFRA_DIR/services/gateway/scripts/uninstall.sh" ]]; then
      "$AI_INFRA_DIR/services/gateway/scripts/uninstall.sh" || true
    fi
    if [[ -x "$AI_INFRA_DIR/services/ollama/scripts/uninstall.sh" ]]; then
      "$AI_INFRA_DIR/services/ollama/scripts/uninstall.sh" || true
    fi
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir|--ai-infra-dir|--nexus-dir)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }
      case "$1" in
        --backup-dir) BACKUP_DIR="$2" ;;
        --ai-infra-dir) AI_INFRA_DIR="$2" ;;
        --nexus-dir) NEXUS_DIR="$2" ;;
      esac
      shift 2
      ;;
    --skip-deploy) SKIP_DEPLOY=1; shift ;;
    --skip-restore) SKIP_RESTORE=1; shift ;;
    --yes) NO_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$AI_INFRA_DIR" ]]; then
  AI_INFRA_DIR="$HOME/ai-infra"
fi
if [[ -z "$BACKUP_DIR" ]]; then
  BACKUP_DIR="$HOME/nexus-migration-backup"
fi

require_cmd docker
require_cmd curl
require_cmd openssl
require_dir "$AI_INFRA_DIR" "ai-infra"
require_dir "$NEXUS_DIR" "nexus"

AI_INFRA_DIR="$(realpath "$AI_INFRA_DIR")"
NEXUS_DIR="$(realpath "$NEXUS_DIR")"
BACKUP_DIR="$(realpath -m "$BACKUP_DIR")"

echo "Nexus migration helper (interactive)"
echo "ai-infra: $AI_INFRA_DIR"
echo "nexus: $NEXUS_DIR"
echo "backups: $BACKUP_DIR"

backup_ai_infra
prepare_nexus

if [[ "$SKIP_RESTORE" -eq 0 ]]; then
  restore_into_nexus
fi

verify_migration
stop_legacy_services

echo "Migration script completed."
echo "Review logs with: docker compose logs --tail=100 gateway ollama"
