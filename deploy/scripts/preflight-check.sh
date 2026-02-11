#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

failures=0
warnings=0

ok() { echo "[OK] $1"; }
warn() { echo "[WARN] $1"; warnings=$((warnings+1)); }
fail() { echo "[FAIL] $1"; failures=$((failures+1)); }

check_cmd() {
  local cmd="$1"
  local name="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$name found"
  else
    fail "$name missing"
  fi
}

echo "Nexus preflight checks"
check_cmd docker "Docker"
check_cmd curl "curl"
check_cmd openssl "openssl"
check_cmd python3 "python3"

if docker info >/dev/null 2>&1; then
  ok "Docker daemon reachable"
else
  fail "Docker daemon not reachable"
fi

if docker compose version >/dev/null 2>&1; then
  ok "Docker Compose available"
else
  fail "Docker Compose unavailable"
fi

for path in services/gateway/Dockerfile services/gateway/app/main.py docker-compose.yml; do
  if [[ -f "$path" ]]; then
    ok "Required file present: $path"
  else
    fail "Required file missing: $path"
  fi
done

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

if [[ -f deploy/env/.env.dev ]]; then
  perms=$(stat -c '%a' deploy/env/.env.dev 2>/dev/null || stat -f '%Lp' deploy/env/.env.dev)
  if [[ "$perms" -le 600 ]]; then
    ok "deploy/env/.env.dev permissions look safe ($perms)"
  else
    warn "deploy/env/.env.dev permissions are broad ($perms), expected 600 or tighter"
  fi
else
  warn "deploy/env/.env.dev not found (expected on deployment host)"
fi

if [[ -f deploy/env/.env.prod ]]; then
  perms=$(stat -c '%a' deploy/env/.env.prod 2>/dev/null || stat -f '%Lp' deploy/env/.env.prod)
  if [[ "$perms" -le 600 ]]; then
    ok "deploy/env/.env.prod permissions look safe ($perms)"
  else
    warn "deploy/env/.env.prod permissions are broad ($perms), expected 600 or tighter"
  fi
else
  warn "deploy/env/.env.prod not found (expected on deployment host)"
fi

echo
if [[ $failures -gt 0 ]]; then
  echo "Preflight completed with $failures failure(s) and $warnings warning(s)."
  exit 1
fi

echo "Preflight completed with $warnings warning(s)."
