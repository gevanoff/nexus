#!/usr/bin/env bash

# If someone runs this via `sh` (or another non-bash shell), fail fast with a clear message.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "[FAIL] This script must be run with bash (not sh/PowerShell)."
  echo "[INFO] On Windows, run it from WSL: ./deploy/scripts/preflight-check.sh"
  exit 2
fi

set -euo pipefail
umask 077

# Maintainer note:
# This script intentionally uses shared helpers from deploy/scripts/_common.sh.
# Keep the [OK]/[WARN]/[FAIL] output stable, and add cross-script helpers in
# _common.sh rather than duplicating logic here.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

mode="default"
env_file_arg=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      mode="${2:-default}"
      shift 2 || true
      ;;
    --env-file)
      env_file_arg="${2:-}"
      shift 2 || true
      ;;
    -h|--help)
      echo "Usage: deploy/scripts/preflight-check.sh [--mode <default|quickstart|deploy>] [--env-file PATH]"
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

failures=0
warnings=0

# Track common blockers so we can print actionable follow-ups.
missing_docker="false"
missing_compose="false"
docker_daemon_ok="false"
missing_env="false"
missing_lsof="false"

ok() { echo "[OK] $1"; }
warn() { echo "[WARN] $1"; warnings=$((warnings+1)); }
fail() { echo "[FAIL] $1"; failures=$((failures+1)); }

check_cmd() {
  local cmd="$1"
  local name="$2"
  if ns_have_cmd "$cmd"; then
    ok "$name found"
  else
    fail "$name missing"
  fi
}

check_cmd_optional() {
  local cmd="$1"
  local name="$2"
  if ns_have_cmd "$cmd"; then
    ok "$name found"
  else
    warn "$name missing (optional)"
  fi
}

echo "Nexus preflight checks"
platform="$(ns_detect_platform)"
check_cmd docker "Docker"
check_cmd curl "curl"
check_cmd lsof "lsof"
check_cmd_optional openssl "openssl"
check_cmd_optional python3 "python3"

if ! ns_have_cmd lsof; then
  missing_lsof="true"
  echo
  echo "Next steps (required)"
  echo "  1) Install lsof: ./deploy/scripts/install-host-deps.sh"
  echo "  2) Re-run: ./deploy/scripts/preflight-check.sh --mode ${mode}"
  echo
  echo "Preflight completed with $failures failure(s) and $warnings warning(s)."
  exit 1
fi

if ! ns_have_cmd docker; then
  missing_docker="true"
fi

if docker info >/dev/null 2>&1; then
  ok "Docker daemon reachable"
  docker_daemon_ok="true"
else
  fail "Docker daemon not reachable"
  if [[ "$platform" == "macos" ]]; then
    if ns_have_cmd colima; then
      warn "macOS: if using Colima, start it with: colima start"
    else
      warn "macOS: start your Docker backend (Colima or Docker Desktop)"
    fi
  fi
fi

compose_cmd=""
compose_cmd="$(ns_compose_cmd_string 2>/dev/null || true)"
if [[ -n "${compose_cmd:-}" ]]; then
  ok "Docker Compose available (${compose_cmd})"
else
  fail "Docker Compose unavailable"
  missing_compose="true"
  if [[ "$platform" == "macos" ]]; then
    warn "macOS: install Compose (either 'docker compose' plugin or 'docker-compose' binary), then retry."
  fi
fi

for path in services/gateway/Dockerfile docker-compose.gateway.yml; do
  if [[ -f "$path" ]]; then
    ok "Required file present: $path"
  else
    fail "Required file missing: $path"
  fi
done

if [[ -f "services/gateway/app/tools_registry.json" ]]; then
  ok "Gateway tools registry present: services/gateway/app/tools_registry.json"
else
  fail "Missing gateway tools registry: services/gateway/app/tools_registry.json"
fi

if [[ -f "services/gateway/app/main.py" && -f "services/gateway/app/requirements.freeze.txt" ]]; then
  ok "Gateway source present: services/gateway/app"
else
  fail "Gateway source missing (expected services/gateway/app with requirements.freeze.txt)"
fi

for service in images tts; do
  if [[ -f "services/$service/Dockerfile" ]]; then
    ok "Optional service buildable: $service"
  else
    warn "Optional service missing Dockerfile: $service (profile/full starts may fail)"
  fi
done

for script in deploy/scripts/deploy.sh deploy/scripts/remote-deploy.sh deploy/scripts/register-service.sh deploy/scripts/list-services.sh quickstart.sh; do
  if [[ -x "$script" ]]; then
    ok "Executable bit set: $script"
  else
    warn "Executable bit not set: $script"
  fi
done

if [[ -f .env.example ]]; then
  ok "Config template present: .env.example"
else
  fail "Missing .env.example (expected at repo root). Re-clone repo or restore file."
fi

check_env_file_perms() {
  local path="$1"
  local label="$2"

  if [[ ! -f "$path" ]]; then
    return 0
  fi

  local perms
  perms="$(ns_stat_perms "$path" 2>/dev/null || true)"
  if [[ -z "${perms:-}" ]]; then
    warn "${label} permissions: unable to determine"
    return 0
  fi

  if [[ "$perms" -le 600 ]]; then
    ok "${label} permissions look safe (${perms})"
    return 0
  fi

  if [[ "$mode" == "deploy" ]]; then
    fail "${label} permissions are broad (${perms}), expected 600 or tighter"
  else
    warn "${label} permissions are broad (${perms}), expected 600 or tighter"
  fi
}

has_any_env="false"
existing_envs=()
if [[ -f .env ]]; then
  has_any_env="true"
  existing_envs+=(".env")
  ok "Config present: .env"
  check_env_file_perms ".env" ".env"
fi

if [[ -f deploy/env/.env.dev || -f deploy/env/.env.prod ]]; then
  has_any_env="true"
  if [[ -f deploy/env/.env.dev ]]; then
    existing_envs+=("deploy/env/.env.dev")
    ok "Host env present: deploy/env/.env.dev"
  fi
  if [[ -f deploy/env/.env.prod ]]; then
    existing_envs+=("deploy/env/.env.prod")
    ok "Host env present: deploy/env/.env.prod"
  fi
  [[ -f deploy/env/.env.dev ]] && check_env_file_perms "deploy/env/.env.dev" "deploy/env/.env.dev"
  [[ -f deploy/env/.env.prod ]] && check_env_file_perms "deploy/env/.env.prod" "deploy/env/.env.prod"
fi

# In deploy mode, match deploy.sh behavior: it will refuse to run with an env file
# that has permissions broader than 600. We can only be strict if we know which env
# file will be used.
if [[ "$mode" == "deploy" ]]; then
  if [[ -n "${env_file_arg:-}" ]]; then
    if [[ -f "$env_file_arg" ]]; then
      check_env_file_perms "$env_file_arg" "$env_file_arg"
    else
      # Not an error: deploy.sh will create it (chmod 600) if missing.
      warn "Env file path provided but not present yet: $env_file_arg (deploy will create it)"
    fi
  else
    if [[ ${#existing_envs[@]} -gt 1 ]]; then
      warn "Multiple env files found; preflight can't know which deploy.sh will use."
      warn "Re-run with: ./deploy/scripts/preflight-check.sh --mode deploy --env-file <path>"
    elif [[ ${#existing_envs[@]} -eq 1 ]]; then
      # If there's exactly one env file in play, be strict about it.
      check_env_file_perms "${existing_envs[0]}" "${existing_envs[0]}"
    fi
  fi
fi

if [[ "$has_any_env" != "true" ]]; then
  missing_env="true"
  case "$mode" in
    quickstart)
      warn "Missing .env (quickstart will create it from .env.example)"
      ;;
    deploy)
      warn "No env file found (.env or deploy/env/.env.dev|.env.prod)."
      warn "Create one from .env.example, or pass --env-file to deploy/scripts/deploy.sh."
      ;;
    *)
      warn "Missing .env. Create it with: cp .env.example .env"
      ;;
  esac
fi

echo
echo "Port checks"

env_file_for_ports="${env_file_arg:-}"
if [[ -z "${env_file_for_ports:-}" ]]; then
  env_file_for_ports="$(ns_guess_env_file "$ROOT_DIR")"
fi

if [[ -n "${env_file_for_ports:-}" && -f "$env_file_for_ports" ]]; then
  ok "Using env file for ports: ${env_file_for_ports#$ROOT_DIR/}"
else
  warn "No env file provided/found; using default ports from compose"
  env_file_for_ports=""
fi

check_port_required() {
  local key="$1"
  local default_port="$2"
  local label="$3"

  local port
  port="$(ns_env_get "$env_file_for_ports" "$key" "$default_port")"

  if ! ns_is_valid_port "$port"; then
    warn "${label}: invalid ${key} value '${port}' (expected 1-65535)"
    return 0
  fi

  local find_cmd
  find_cmd="$(ns_port_find_listener_cmd "$port")"

  local rc
  ns_port_in_use "$port"; rc=$?
  if [[ $rc -eq 0 ]]; then
    fail "${label}: port ${port} is already in use"
    if [[ -n "${find_cmd:-}" ]]; then
      warn "Find the listener with: ${find_cmd}"
    else
      warn "Install 'lsof' (preferred) to show which process is listening."
    fi
    details="$(ns_port_in_use_details "$port" || true)"
    if [[ -n "${details:-}" ]]; then
      warn "Listener (best-effort):"
      while IFS= read -r line; do
        [[ -z "${line:-}" ]] && continue
        warn "  ${line}"
      done <<<"$details"
    fi
  elif [[ $rc -eq 1 ]]; then
    ok "${label}: port ${port} looks free"
  else
    warn "${label}: unable to check port ${port} (missing lsof/ss/netstat?)"
  fi
}

check_port_optional() {
  local key="$1"
  local default_port="$2"
  local label="$3"

  local port
  port="$(ns_env_get "$env_file_for_ports" "$key" "$default_port")"

  if ! ns_is_valid_port "$port"; then
    warn "${label}: invalid ${key} value '${port}' (expected 1-65535)"
    return 0
  fi

  local rc
  ns_port_in_use "$port"; rc=$?
  if [[ $rc -eq 0 ]]; then
    warn "${label}: port ${port} is already in use (this only matters if you enable that service/profile)"
  elif [[ $rc -eq 1 ]]; then
    ok "${label}: port ${port} looks free"
  else
    warn "${label}: unable to check port ${port} (missing lsof/ss/netstat?)"
  fi
}

# Core services (started by default)
check_port_required GATEWAY_PORT 8800 "Gateway API"
check_port_required OBSERVABILITY_PORT 8801 "Gateway observability"
check_port_required OLLAMA_PORT 11434 "Ollama"
check_port_required ETCD_PORT 2379 "etcd"

# Optional services (separate compose files)
check_port_optional IMAGES_PORT 7860 "Images service"
check_port_optional TTS_PORT 9940 "TTS service"

echo
echo "Next steps (suggested order)"
if [[ "$missing_docker" == "true" ]]; then
  echo "  1) Install Docker/Colima prerequisites: ./deploy/scripts/install-host-deps.sh"
elif [[ "$docker_daemon_ok" != "true" ]]; then
  if [[ "$platform" == "macos" ]]; then
    if ns_have_cmd colima; then
      echo "  1) Start Colima: colima start"
    else
      echo "  1) Start your Docker backend (Colima or Docker Desktop)"
    fi
  else
    echo "  1) Start Docker, then re-run preflight"
  fi
else
  echo "  1) Docker looks OK"
fi

if [[ "$missing_compose" == "true" ]]; then
  echo "  2) Install Docker Compose: ./deploy/scripts/install-host-deps.sh"
else
  echo "  2) Docker Compose looks OK"
fi

if [[ "$missing_lsof" == "true" ]]; then
  echo "  3) Install lsof (required for port diagnostics): ./deploy/scripts/install-host-deps.sh"
  echo "  4) Re-run: ./deploy/scripts/preflight-check.sh --mode ${mode}"
  echo "  5) Deploy:  ./deploy/scripts/deploy.sh dev main   (or prod)"
  echo "  6) Verify:  ./deploy/scripts/verify-gateway.sh && ./deploy/scripts/smoke-test-gateway.sh"
  echo
  echo "Preflight completed with $failures failure(s) and $warnings warning(s)."
  exit 1
fi

if [[ "$missing_env" == "true" ]]; then
  echo "  3) Create config: ./deploy/scripts/import-env.sh   (or: cp .env.example .env)"
else
  echo "  3) Config file looks OK"
fi

if [[ "$missing_docker" == "true" || "$missing_compose" == "true" || "$missing_env" == "true" || "$docker_daemon_ok" != "true" ]]; then
  echo "  4) After fixing items above, re-run: ./deploy/scripts/preflight-check.sh --mode ${mode}"
else
  echo "  4) Preflight looks good; proceed to deploy"
fi

echo "  5) Deploy:  ./deploy/scripts/deploy.sh dev main   (or prod)"
echo "  6) Verify:  ./deploy/scripts/verify-gateway.sh && ./deploy/scripts/smoke-test-gateway.sh"

echo
if [[ $failures -gt 0 ]]; then
  echo "Preflight completed with $failures failure(s) and $warnings warning(s)."
  exit 1
fi

echo "Preflight completed with $warnings warning(s)."
