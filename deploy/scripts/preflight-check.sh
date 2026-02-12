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
if [[ ${1:-} == "--mode" ]]; then
  mode="${2:-default}"
  shift 2 || true
fi

failures=0
warnings=0

# Track common blockers so we can print actionable follow-ups.
missing_docker="false"
missing_compose="false"
docker_daemon_ok="false"
missing_env="false"

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
check_cmd_optional openssl "openssl"
check_cmd_optional python3 "python3"

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

for path in services/gateway/Dockerfile docker-compose.yml; do
  if [[ -f "$path" ]]; then
    ok "Required file present: $path"
  else
    fail "Required file missing: $path"
  fi
done

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

has_any_env="false"
if [[ -f .env ]]; then
  has_any_env="true"
  ok "Config present: .env"
  perms="$(ns_stat_perms .env)"
  if [[ "$perms" -le 600 ]]; then
    ok ".env permissions look safe ($perms)"
  else
    warn ".env permissions are broad ($perms), expected 600 or tighter"
  fi
fi

if [[ -f deploy/env/.env.dev || -f deploy/env/.env.prod ]]; then
  has_any_env="true"
  [[ -f deploy/env/.env.dev ]] && ok "Host env present: deploy/env/.env.dev"
  [[ -f deploy/env/.env.prod ]] && ok "Host env present: deploy/env/.env.prod"
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

if [[ "$missing_env" == "true" ]]; then
  echo "  3) Create config: ./deploy/scripts/import-env.sh   (or: cp .env.example .env)"
else
  echo "  3) Config file looks OK"
fi

echo "  4) Re-run: ./deploy/scripts/preflight-check.sh --mode ${mode}"
echo "  5) Deploy:  ./deploy/scripts/deploy.sh dev main   (or prod)"
echo "  6) Verify:  ./deploy/scripts/verify-gateway.sh && ./deploy/scripts/smoke-test-gateway.sh"

echo
if [[ $failures -gt 0 ]]; then
  echo "Preflight completed with $failures failure(s) and $warnings warning(s)."
  exit 1
fi

echo "Preflight completed with $warnings warning(s)."
