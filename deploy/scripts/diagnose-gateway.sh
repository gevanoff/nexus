#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
gateway_port="${GATEWAY_PORT:-}"
obs_port="${OBSERVABILITY_PORT:-}"
if [[ -f "${ENV_FILE}" ]]; then
  gateway_port="${gateway_port:-$(ns_env_get "${ENV_FILE}" GATEWAY_PORT 8800)}"
  obs_port="${obs_port:-$(ns_env_get "${ENV_FILE}" OBSERVABILITY_PORT 8801)}"
fi

gateway_port="${gateway_port:-8800}"
obs_port="${obs_port:-8801}"

BASE_URL="${GATEWAY_BASE_URL:-http://127.0.0.1:${gateway_port}}"
OBS_URL="${GATEWAY_OBS_URL:-http://127.0.0.1:${obs_port}}"
TOKEN="${GATEWAY_BEARER_TOKEN:-}"
if [[ -z "${TOKEN}" && -f "${ENV_FILE}" ]]; then
  TOKEN="$(ns_env_get "${ENV_FILE}" GATEWAY_BEARER_TOKEN "")"
fi
embeddings_model="${EMBEDDINGS_MODEL:-}"
if [[ -z "${embeddings_model}" && -f "${ENV_FILE}" ]]; then
  embeddings_model="$(ns_env_get "${ENV_FILE}" EMBEDDINGS_MODEL "nomic-embed-text")"
fi
embeddings_model="${embeddings_model:-nomic-embed-text}"

ollama_model_fast="${OLLAMA_MODEL_FAST:-}"
if [[ -z "${ollama_model_fast}" && -f "${ENV_FILE}" ]]; then
  ollama_model_fast="$(ns_env_get "${ENV_FILE}" OLLAMA_MODEL_FAST "qwen2.5:7b")"
fi
ollama_model_fast="${ollama_model_fast:-qwen2.5:7b}"

ollama_model_strong="${OLLAMA_MODEL_STRONG:-}"
if [[ -z "${ollama_model_strong}" && -f "${ENV_FILE}" ]]; then
  ollama_model_strong="$(ns_env_get "${ENV_FILE}" OLLAMA_MODEL_STRONG "qwen2.5:32b")"
fi
ollama_model_strong="${ollama_model_strong:-qwen2.5:32b}"

# SYNC-CHECK(core-compose-files): keep aligned with ops-stack.sh and cutover-one-way.sh.
COMPOSE_ARGS=(-f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml)

rc=0

mark_fail() {
  rc=1
}

print_step() {
  echo
  ns_print_header "$1"
}

http_check() {
  # Usage: http_check <label> <method> <url> <auth:true|false> [json_payload]
  local label="$1"
  local method="$2"
  local url="$3"
  local with_auth="$4"
  local payload="${5:-}"
  local tmp
  local status
  local body_preview

  tmp="$(mktemp)"

  if [[ "$method" == "GET" ]]; then
    if [[ "$with_auth" == "true" ]]; then
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "Authorization: Bearer ${TOKEN}" || true)"
    else
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" || true)"
    fi
  else
    if [[ "$with_auth" == "true" ]]; then
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -X "$method" -d "$payload" || true)"
    else
      status="$(curl -sS -o "$tmp" -w "%{http_code}" "$url" -H "Content-Type: application/json" -X "$method" -d "$payload" || true)"
    fi
  fi

  if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
    ns_print_ok "$label -> HTTP $status"
    rm -f "$tmp"
    return 0
  fi

  ns_print_error "$label -> HTTP $status"
  if [[ -s "$tmp" ]]; then
    body_preview="$(head -c 600 "$tmp" 2>/dev/null || true)"
    ns_print_warn "Body (first 600 chars):"
    echo "$body_preview"
    echo
  fi
  rm -f "$tmp"

  if [[ "$status" == "403" && "${body_preview:-}" == *"Client IP not allowed"* ]]; then
    ns_print_warn "IP allowlist rejection detected."
    ns_print_warn "Set IP_ALLOWLIST in ${ENV_FILE} to include your client IP/CIDR, then restart stack."
    ns_print_warn "Common Docker bridge peer for this stack: 172.28.0.1"
  elif [[ "$status" == "401" || "$status" == "403" ]]; then
    ns_print_warn "Auth failure: token may be wrong for the running gateway instance."
    ns_print_warn "Token source: ${ENV_FILE} (or GATEWAY_BEARER_TOKEN env var)."
  fi

  mark_fail
  return 1
}

ollama_model_present() {
  # Usage: ollama_model_present <model>
  local model="$1"
  local escaped
  escaped="$(printf '%s' "$model" | sed 's/[][(){}.^$*+?|\\/]/\\&/g')"
  # Accept exact model name with either explicit tag suffix or bare name column.
  ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T ollama ollama list 2>/dev/null |
    grep -E "^${escaped}(:[^[:space:]]+)?[[:space:]]" >/dev/null 2>&1
}

print_step "Gateway Diagnostics"
echo "Repo root: ${ROOT_DIR}"
echo "Env file: ${ENV_FILE}"
echo "Base URL: ${BASE_URL}"
echo "Observability URL: ${OBS_URL}"

print_step "Host prerequisites"
if ! ns_have_cmd docker; then
  ns_print_error "docker CLI not found"
  mark_fail
else
  ns_print_ok "docker CLI found"
fi

if ! ns_compose_available; then
  ns_print_error "Docker Compose not available"
  mark_fail
else
  ns_print_ok "Docker Compose available: $(ns_compose_cmd_string)"
fi

if ! docker info >/dev/null 2>&1; then
  ns_print_error "Docker daemon not reachable"
  ns_print_warn "Try: ./deploy/scripts/ops-stack.sh"
  mark_fail
else
  ns_print_ok "Docker daemon reachable"
fi

print_step "Compose files and stack state"
for compose_file in docker-compose.gateway.yml docker-compose.ollama.yml docker-compose.etcd.yml; do
  if [[ -f "$ROOT_DIR/$compose_file" ]]; then
    ns_print_ok "Found $compose_file"
  else
    ns_print_error "Missing $compose_file"
    mark_fail
  fi
done

if ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" config >/dev/null 2>&1; then
  ns_print_ok "Compose config resolves"
else
  ns_print_error "Compose config resolution failed"
  ns_print_warn "Check ENV_FILE (${ENV_FILE}) and compose file references"
  mark_fail
fi

ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" ps || mark_fail

print_step "HTTP endpoint checks"
http_check "GET ${OBS_URL}/health" "GET" "${OBS_URL}/health" "false"

if [[ -z "${TOKEN}" ]]; then
  ns_print_error "GATEWAY_BEARER_TOKEN not found in env or ${ENV_FILE}"
  mark_fail
else
  ns_print_ok "Bearer token present"
fi

http_check "GET ${BASE_URL}/v1/models" "GET" "${BASE_URL}/v1/models" "true"
http_check "GET ${BASE_URL}/v1/gateway/status" "GET" "${BASE_URL}/v1/gateway/status" "true"
http_check "POST ${BASE_URL}/v1/embeddings" "POST" "${BASE_URL}/v1/embeddings" "true" '{"model":"default","input":"diagnose"}'

print_step "MLX readiness (from gateway container)"
if ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T gateway python3 - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

base = (os.getenv("MLX_BASE_URL") or "http://mlx:10240/v1").rstrip("/")
url = f"{base}/models"
start = time.time()
try:
  req = urllib.request.Request(url, method="GET")
  with urllib.request.urlopen(req, timeout=5) as resp:
    status = int(resp.getcode() or 0)
    body = resp.read().decode("utf-8", errors="replace")

  model_count = 0
  try:
    payload = json.loads(body)
    data = payload.get("data")
    if isinstance(data, list):
      model_count = len(data)
  except Exception:
    pass

  elapsed_ms = int((time.time() - start) * 1000)
  print(f"mlx_url={url}")
  print(f"mlx_status={status}")
  print(f"mlx_model_count={model_count}")
  print(f"mlx_probe_ms={elapsed_ms}")
  sys.exit(0 if status == 200 else 1)
except Exception as exc:
  elapsed_ms = int((time.time() - start) * 1000)
  print(f"mlx_url={url}")
  print(f"mlx_probe_ms={elapsed_ms}")
  print(f"mlx_error={exc}")
  sys.exit(1)
PY
then
  ns_print_ok "MLX /models probe from gateway container succeeded"
else
  ns_print_error "MLX /models probe from gateway container failed"
  ns_print_warn "Check MLX container/service availability and MLX_BASE_URL in ${ENV_FILE}."
  mark_fail
fi

print_step "Backend access matrix (from gateway container)"
if ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T gateway python3 - <<'PY'
import sys
import time
import urllib.request

from app.backends import get_registry
from app.health_checker import get_health_checker


def probe(url: str, timeout: float = 5.0) -> tuple[int, str, int]:
  started = time.time()
  status = 0
  err = ""
  try:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      status = int(resp.getcode() or 0)
  except Exception as exc:
    err = f"{type(exc).__name__}: {exc}"
  elapsed_ms = int((time.time() - started) * 1000)
  return status, err, elapsed_ms


registry = get_registry()
checker = get_health_checker()

print("backend_access_header=backend|liveness|readyz|checker|base_url")
failures = 0

for backend_class, cfg in sorted(registry.backends.items(), key=lambda kv: kv[0]):
  base = (cfg.base_url or "").rstrip("/")
  checker_state = checker.get_status(backend_class)
  checker_repr = "unknown"
  if checker_state is not None:
    checker_repr = f"healthy={checker_state.is_healthy},ready={checker_state.is_ready}"
    if checker_state.error:
      checker_repr += f",error={checker_state.error}"

  if not base or not (base.startswith("http://") or base.startswith("https://")):
    print(f"backend_access={backend_class}|skip(base_url_unset)|skip(base_url_unset)|{checker_repr}|{base}")
    continue

  live_url = f"{base}{cfg.health_liveness}"
  ready_url = f"{base}{cfg.health_readiness}"
  live_status, live_err, live_ms = probe(live_url)
  ready_status, ready_err, ready_ms = probe(ready_url)

  live_repr = f"{live_status}({live_ms}ms)" if not live_err else f"err({live_ms}ms):{live_err}"
  ready_repr = f"{ready_status}({ready_ms}ms)" if not ready_err else f"err({ready_ms}ms):{ready_err}"

  print(f"backend_access={backend_class}|{live_repr}|{ready_repr}|{checker_repr}|{base}")

  if live_status != 200 or ready_status != 200 or live_err or ready_err:
    failures += 1

if failures:
  print(f"backend_access_failures={failures}")
  sys.exit(1)

print("backend_access_failures=0")
PY
then
  ns_print_ok "Configured backend probes succeeded"
else
  ns_print_error "One or more configured backend probes failed"
  ns_print_warn "Review backend_access lines above to identify failing backend class and endpoint"
  mark_fail
fi

print_step "Ollama embeddings model readiness"
echo "Expected embeddings model: ${embeddings_model}"
if ollama_model_present "$embeddings_model"; then
  ns_print_ok "Embeddings model is present in Ollama"
else
  ns_print_error "Embeddings model not present in Ollama: ${embeddings_model}"
  ns_print_warn "Pull it with:"
  ns_print_warn "  docker-compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml exec -T ollama ollama pull ${embeddings_model}"
  mark_fail
fi

print_step "Ollama chat model readiness"
echo "Expected chat models:"
echo "  fast=${ollama_model_fast}"
echo "  strong=${ollama_model_strong}"

if ollama_model_present "$ollama_model_fast"; then
  ns_print_ok "Fast chat model is present"
else
  ns_print_error "Fast chat model not present: ${ollama_model_fast}"
  ns_print_warn "Pull it with:"
  ns_print_warn "  docker-compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml exec -T ollama ollama pull ${ollama_model_fast}"
  mark_fail
fi

if ollama_model_present "$ollama_model_strong"; then
  ns_print_ok "Strong chat model is present"
else
  ns_print_error "Strong chat model not present: ${ollama_model_strong}"
  ns_print_warn "Pull it with:"
  ns_print_warn "  docker-compose -f docker-compose.gateway.yml -f docker-compose.ollama.yml -f docker-compose.etcd.yml exec -T ollama ollama pull ${ollama_model_strong}"
  mark_fail
fi

print_step "Verifier run (in-container)"
if [[ -n "${TOKEN}" ]]; then
  if ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" exec -T gateway \
    python3 /var/lib/gateway/tools/verify_gateway.py \
    --skip-pytest \
    --base-url "http://127.0.0.1:${gateway_port}" \
    --obs-url "http://127.0.0.1:${obs_port}" \
    --token "${TOKEN}"; then
    ns_print_ok "In-container verifier passed"
  else
    ns_print_error "In-container verifier failed"
    mark_fail
  fi
else
  ns_print_warn "Skipping in-container verifier (missing token)"
fi

print_step "Gateway logs (tail 120)"
ns_compose --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" logs --tail=120 gateway || true

echo
if [[ "$rc" -eq 0 ]]; then
  ns_print_ok "Gateway diagnostics completed without detected issues"
else
  ns_print_error "Gateway diagnostics found issues"
  exit 1
fi
