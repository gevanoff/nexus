#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
CHECK_ONLY="false"
WITH_MLX="false"
EXTERNAL_OLLAMA="false"
OLLAMA_BASE_URL_OVERRIDE="${PREWARM_OLLAMA_BASE_URL:-}"
EXTERNAL_OLLAMA_SET="false"
FROM_ALIASES="false"
ALIASES_FILE="${ROOT_DIR}/.runtime/gateway/config/model_aliases.json"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/prewarm-models.sh [--env-file PATH] [--check-only] [--external-ollama] [--ollama-base-url URL] [--model MODEL] [--from-aliases] [--aliases-file PATH]

Idempotently checks required Ollama models and pulls only missing ones.
Required models are derived from env (or defaults):
  - EMBEDDINGS_MODEL when EMBEDDINGS_BACKEND uses Ollama (default: nomic-embed-text)
  - OLLAMA_MODEL_FAST (default: qwen2.5:7b)
  - OLLAMA_MODEL_STRONG (default: qwen2.5:32b)

Options:
  --env-file PATH   Env file path (default: ./.env)
  --check-only      Check/report only; do not pull missing models
  --with-mlx        Include MLX component (docker-compose.mlx.yml) in compose checks
  --external-ollama Use external/native Ollama via OLLAMA_BASE_URL (no ollama container)
  --ollama-base-url URL
                    Explicit URL for prewarm target (overrides OLLAMA_BASE_URL);
                    also supported via PREWARM_OLLAMA_BASE_URL env var.
  --model MODEL     Add an explicit model to warm/check (repeatable)
  --from-aliases    Include all backend=ollama models from model_aliases.json
  --aliases-file PATH
                    Alias config path (default: ./.runtime/gateway/config/model_aliases.json)
EOF
}

declare -a EXPLICIT_MODELS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --check-only)
      CHECK_ONLY="true"
      shift
      ;;
    --with-mlx)
      WITH_MLX="true"
      shift
      ;;
    --external-ollama)
      EXTERNAL_OLLAMA="true"
      EXTERNAL_OLLAMA_SET="true"
      shift
      ;;
    --ollama-base-url)
      OLLAMA_BASE_URL_OVERRIDE="${2:-}"
      shift 2
      ;;
    --model)
      EXPLICIT_MODELS+=("${2:-}")
      shift 2
      ;;
    --from-aliases)
      FROM_ALIASES="true"
      shift
      ;;
    --aliases-file)
      ALIASES_FILE="${2:-}"
      FROM_ALIASES="true"
      shift 2
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

if [[ ! -f "$ENV_FILE" ]]; then
  ns_print_warn "Env file not found at $ENV_FILE; creating from .env.example"
  ns_ensure_env_file "$ENV_FILE" "$ROOT_DIR"
fi

if [[ "$EXTERNAL_OLLAMA_SET" != "true" ]]; then
  autodetect_ollama_base_url="$(ns_env_get "$ENV_FILE" OLLAMA_BASE_URL "http://ollama:11434")"
  autodetect_ollama_base_url="${autodetect_ollama_base_url%/}"
  if [[ "$autodetect_ollama_base_url" != "http://ollama:11434" ]]; then
    EXTERNAL_OLLAMA="true"
  fi
fi

# SYNC-CHECK(core-compose-files): keep aligned with ops-stack.sh and cutover-one-way.sh.
COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.etcd.yml)
COMPOSE_FILES=(docker-compose.gateway.yml docker-compose.etcd.yml)
if [[ "$EXTERNAL_OLLAMA" != "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.ollama.yml)
  COMPOSE_FILES+=(docker-compose.ollama.yml)
fi
if [[ "$WITH_MLX" == "true" ]]; then
  COMPOSE_ARGS+=(-f docker-compose.mlx.yml)
  COMPOSE_FILES+=(docker-compose.mlx.yml)
fi

for compose_file in "${COMPOSE_FILES[@]}"; do
  if [[ ! -f "$ROOT_DIR/$compose_file" ]]; then
    ns_die "Compose file not found: $ROOT_DIR/$compose_file"
  fi
done

ns_ensure_project_env_bind_source "$ROOT_DIR" "$ENV_FILE"

ns_print_header "Prewarm Ollama models"

if [[ "$EXTERNAL_OLLAMA" != "true" ]]; then
  ns_require_cmd docker || exit 1
  if ! ns_compose_available; then
    ns_die "Docker Compose is not available"
  fi
  if ! ns_ensure_docker_daemon true; then
    ns_die "Docker daemon is not reachable"
  fi
else
  ns_require_cmd python3 || exit 1
fi

if [[ "$FROM_ALIASES" == "true" ]]; then
  ns_require_cmd python3 || exit 1
fi

if [[ -n "${OLLAMA_BASE_URL_OVERRIDE:-}" ]]; then
  ollama_base_url="$OLLAMA_BASE_URL_OVERRIDE"
else
  ollama_base_url="${OLLAMA_BASE_URL:-$(ns_env_get "$ENV_FILE" OLLAMA_BASE_URL "http://ollama:11434")}"
fi
ollama_base_url="${ollama_base_url%/}"
ollama_tags_url="${ollama_base_url}/api/tags"

if [[ "$EXTERNAL_OLLAMA" != "true" ]]; then
  # Ensure ollama service exists and is started.
  if ! ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" ps ollama >/dev/null 2>&1; then
    ns_die "Compose could not resolve service 'ollama'."
  fi
  ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" up -d ollama >/dev/null
fi

embeddings_backend="${EMBEDDINGS_BACKEND:-$(ns_env_get "$ENV_FILE" EMBEDDINGS_BACKEND "local_mlx")}"
embeddings_model="${EMBEDDINGS_MODEL:-$(ns_env_get "$ENV_FILE" EMBEDDINGS_MODEL "")}"
ollama_model_fast="${OLLAMA_MODEL_FAST:-$(ns_env_get "$ENV_FILE" OLLAMA_MODEL_FAST "qwen2.5:7b")}"
ollama_model_strong="${OLLAMA_MODEL_STRONG:-$(ns_env_get "$ENV_FILE" OLLAMA_MODEL_STRONG "qwen2.5:32b")}"

declare -a required_models=()
add_unique_model() {
  local candidate="$1"
  [[ -n "${candidate:-}" ]] || return 0
  local existing
  for existing in "${required_models[@]:-}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return 0
    fi
  done
  required_models+=("$candidate")
}

if [[ "${embeddings_backend}" == ollama* ]]; then
  add_unique_model "${embeddings_model:-nomic-embed-text}"
fi
add_unique_model "$ollama_model_fast"
add_unique_model "$ollama_model_strong"

for explicit_model in "${EXPLICIT_MODELS[@]:-}"; do
  add_unique_model "$explicit_model"
done

if [[ "$FROM_ALIASES" == "true" ]]; then
  if [[ ! -f "$ALIASES_FILE" ]]; then
    ns_die "Alias file not found: $ALIASES_FILE"
  fi
  while IFS= read -r alias_model; do
    add_unique_model "$alias_model"
  done < <(python3 - "$ALIASES_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

for alias in payload.get("aliases", {}).values():
    if not isinstance(alias, dict):
        continue
    if alias.get("backend") != "ollama":
        continue
    model = str(alias.get("model", "")).strip()
    if model:
        print(model)
PY
)
fi

if [[ "${#required_models[@]}" -eq 0 ]]; then
  ns_die "No required models were resolved from environment/defaults"
fi

echo "Required models: ${required_models[*]}"

escape_ere() {
  printf '%s' "$1" | sed 's/[][(){}.^$*+?|\\/]/\\&/g'
}

if [[ "$EXTERNAL_OLLAMA" == "true" ]]; then
  ns_print_warn "Using external/native Ollama endpoint: ${ollama_base_url}"
  list_output="$(curl -fsS "$ollama_tags_url" 2>/dev/null || true)"
  if [[ -z "$list_output" ]]; then
    fallback_base_url="${ollama_base_url/host.docker.internal/127.0.0.1}"
    if [[ "$fallback_base_url" != "$ollama_base_url" ]]; then
      ns_print_warn "Retrying external Ollama endpoint using host-local fallback: ${fallback_base_url}"
      fallback_tags_url="${fallback_base_url}/api/tags"
      list_output="$(curl -fsS "$fallback_tags_url" 2>/dev/null || true)"
      if [[ -n "$list_output" ]]; then
        ollama_base_url="$fallback_base_url"
        ollama_tags_url="$fallback_tags_url"
      fi
    fi
  fi
  if [[ -z "$list_output" ]]; then
    ns_die "Could not reach external Ollama at ${ollama_tags_url}"
  fi
else
  list_output="$(ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T ollama ollama list 2>/dev/null || true)"
fi

model_present_in_list() {
  local model="$1"
  if [[ "$EXTERNAL_OLLAMA" == "true" ]]; then
  python3 - "$model" "$list_output" <<'PY'
import json
import sys

target = sys.argv[1]
data = sys.argv[2]

try:
    payload = json.loads(data)
except Exception:
    sys.exit(1)

for item in payload.get("models", []):
    name = item.get("name", "")
    if name == target or name.startswith(target + ":"):
        sys.exit(0)

sys.exit(1)
PY
  else
    local escaped
    escaped="$(escape_ere "$model")"
    echo "$list_output" | grep -E "^${escaped}(:[^[:space:]]+)?[[:space:]]" >/dev/null 2>&1
  fi
}

declare -a missing_models=()
for model in "${required_models[@]}"; do
  if model_present_in_list "$model"; then
    ns_print_ok "Model present: $model"
  else
    ns_print_warn "Model missing: $model"
    missing_models+=("$model")
  fi
done

if [[ "${#missing_models[@]}" -eq 0 ]]; then
  ns_print_ok "All required models are already present"
  exit 0
fi

if [[ "$CHECK_ONLY" == "true" ]]; then
  ns_print_error "Missing models detected (check-only mode): ${missing_models[*]}"
  exit 1
fi

ns_print_header "Pulling missing models"
for model in "${missing_models[@]}"; do
  ns_print_warn "Pulling $model ..."
  if [[ "$EXTERNAL_OLLAMA" == "true" ]]; then
    curl -fsS -X POST "${ollama_base_url}/api/pull" -H "Content-Type: application/json" -d "{\"model\":\"${model}\"}" >/dev/null
  else
    ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T ollama ollama pull "$model"
  fi
done

if [[ "$EXTERNAL_OLLAMA" == "true" ]]; then
  list_output="$(curl -fsS "$ollama_tags_url" 2>/dev/null || true)"
else
  list_output="$(ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T ollama ollama list 2>/dev/null || true)"
fi

still_missing=0
for model in "${required_models[@]}"; do
  if model_present_in_list "$model"; then
    ns_print_ok "Model ready: $model"
  else
    ns_print_error "Model still missing after pull: $model"
    still_missing=1
  fi
done

if [[ "$still_missing" -ne 0 ]]; then
  exit 1
fi

ns_print_ok "Model prewarm complete"
