#!/usr/bin/env bash
# Shared helpers for Nexus deploy scripts.
# Shellcheck-friendly: this file is sourced, not executed.
# IMPORTANT: Do not modify shell options here (set -e/-u/-o pipefail).
# Entry-point scripts should choose their own strict-mode settings.

# Maintainer note:
# - If you need new behavior in multiple scripts, add a helper here and reuse it.
# - Avoid copying/pasting OS detection, dependency installation, token generation,
#   env-file handling, prompting, or validation logic into individual scripts.
# - Keep helpers small, composable, and side-effect free where possible.

# Colors (best-effort; ok if terminal doesn't support)
_RED='\033[0;31m'
_GREEN='\033[0;32m'
_YELLOW='\033[1;33m'
_NC='\033[0m'

ns_print_header() { echo -e "${_GREEN}=== $1 ===${_NC}"; }
ns_print_error() { echo -e "${_RED}ERROR: $1${_NC}" >&2; }
ns_print_warn() { echo -e "${_YELLOW}WARNING: $1${_NC}" >&2; }
ns_print_ok() { echo -e "${_GREEN}✓ $1${_NC}"; }

ns_have_cmd() { command -v "$1" >/dev/null 2>&1; }

ns_mkdir_p() {
  # Best-effort mkdir -p with helpful error.
  local dir="$1"
  if [[ -z "${dir:-}" ]]; then
    ns_print_error "ns_mkdir_p called with empty path"
    return 1
  fi
  mkdir -p "$dir" 2>/dev/null || {
    ns_print_error "Failed to create directory: $dir"
    return 1
  }
}

ns_ensure_gateway_runtime_dirs() {
  # Create repo-local runtime dirs that are bind-mounted into the gateway container.
  # Usage: ns_ensure_gateway_runtime_dirs <repo_root>
  local repo_root="$1"
  ns_mkdir_p "${repo_root}/.runtime/gateway/config"
  ns_mkdir_p "${repo_root}/.runtime/gateway/data/tools"
  ns_mkdir_p "${repo_root}/.runtime/gateway/data/ui_images"
  ns_mkdir_p "${repo_root}/.runtime/gateway/data/ui_files"
  ns_mkdir_p "${repo_root}/.runtime/gateway/data/ui_chats"
  ns_mkdir_p "${repo_root}/.runtime/gateway/data/tools_work"
}

ns_seed_gateway_config_files() {
  # Seed operator-edited gateway config into .runtime so it survives upgrades.
  # Usage: ns_seed_gateway_config_files <repo_root>
  local repo_root="$1"
  ns_ensure_gateway_runtime_dirs "$repo_root"

  local tools_registry_template="${repo_root}/services/gateway/env/tools_registry.json.example"
  local model_aliases_template="${repo_root}/services/gateway/env/model_aliases.json.example"
  local agent_specs_template="${repo_root}/services/gateway/env/agent_specs.json.example"

  local tools_registry_dst="${repo_root}/.runtime/gateway/config/tools_registry.json"
  local model_aliases_dst="${repo_root}/.runtime/gateway/config/model_aliases.json"
  local agent_specs_dst="${repo_root}/.runtime/gateway/config/agent_specs.json"

  # Migration: older layouts stored these files under .runtime/gateway/data.
  local legacy_tools_registry="${repo_root}/.runtime/gateway/data/tools_registry.json"
  local legacy_model_aliases="${repo_root}/.runtime/gateway/data/model_aliases.json"
  local legacy_agent_specs="${repo_root}/.runtime/gateway/data/agent_specs.json"

  if [[ ! -f "$tools_registry_dst" && -f "$legacy_tools_registry" ]]; then
    cp "$legacy_tools_registry" "$tools_registry_dst" 2>/dev/null || true
  fi
  if [[ ! -f "$model_aliases_dst" && -f "$legacy_model_aliases" ]]; then
    cp "$legacy_model_aliases" "$model_aliases_dst" 2>/dev/null || true
  fi
  if [[ ! -f "$agent_specs_dst" && -f "$legacy_agent_specs" ]]; then
    cp "$legacy_agent_specs" "$agent_specs_dst" 2>/dev/null || true
  fi

  if [[ ! -f "$tools_registry_dst" ]]; then
    if [[ -f "$tools_registry_template" ]]; then
      cp "$tools_registry_template" "$tools_registry_dst" 2>/dev/null || {
        ns_print_error "Failed to seed tools registry: $tools_registry_dst"
        return 1
      }
      chmod 600 "$tools_registry_dst" 2>/dev/null || true
      ns_print_ok "Seeded tools registry: $tools_registry_dst"
    else
      ns_print_warn "Tools registry template not found: $tools_registry_template (skipping seed)"
    fi
  fi

  if [[ ! -f "$model_aliases_dst" ]]; then
    if [[ -f "$model_aliases_template" ]]; then
      cp "$model_aliases_template" "$model_aliases_dst" 2>/dev/null || {
        ns_print_error "Failed to seed model aliases: $model_aliases_dst"
        return 1
      }
      chmod 600 "$model_aliases_dst" 2>/dev/null || true
      ns_print_ok "Seeded model aliases: $model_aliases_dst"
    else
      ns_print_warn "Model aliases template not found: $model_aliases_template (skipping seed)"
    fi
  fi

  if [[ ! -f "$agent_specs_dst" ]]; then
    if [[ -f "$agent_specs_template" ]]; then
      cp "$agent_specs_template" "$agent_specs_dst" 2>/dev/null || {
        ns_print_error "Failed to seed agent specs: $agent_specs_dst"
        return 1
      }
      chmod 600 "$agent_specs_dst" 2>/dev/null || true
      ns_print_ok "Seeded agent specs: $agent_specs_dst"
    else
      ns_print_warn "Agent specs template not found: $agent_specs_template (skipping seed)"
    fi
  fi
}

ns_ensure_ollama_runtime_dirs() {
  # Persist large model blobs and metadata on the host filesystem.
  # Usage: ns_ensure_ollama_runtime_dirs <repo_root>
  local repo_root="$1"
  ns_mkdir_p "${repo_root}/.runtime/ollama"
}

ns_ensure_images_runtime_dirs() {
  # Images service persistence (outputs, caches) and optional model storage.
  # Usage: ns_ensure_images_runtime_dirs <repo_root>
  local repo_root="$1"
  ns_mkdir_p "${repo_root}/.runtime/images/data"
  ns_mkdir_p "${repo_root}/.runtime/images/models"
}

ns_ensure_tts_runtime_dirs() {
  # TTS service persistence (cache/output as needed by backend).
  # Usage: ns_ensure_tts_runtime_dirs <repo_root>
  local repo_root="$1"
  ns_mkdir_p "${repo_root}/.runtime/tts/data"
}

ns_ensure_etcd_runtime_dirs() {
  # Persist etcd state on host so registrations survive container upgrades.
  # Usage: ns_ensure_etcd_runtime_dirs <repo_root>
  local repo_root="$1"
  ns_mkdir_p "${repo_root}/.runtime/etcd/data"
}

ns_ensure_runtime_dirs() {
  # Create all repo-local runtime dirs.
  # Usage: ns_ensure_runtime_dirs <repo_root>
  local repo_root="$1"
  ns_ensure_gateway_runtime_dirs "$repo_root"
  ns_ensure_ollama_runtime_dirs "$repo_root"
  ns_ensure_images_runtime_dirs "$repo_root"
  ns_ensure_tts_runtime_dirs "$repo_root"
  ns_ensure_etcd_runtime_dirs "$repo_root"
}

ns_seed_gateway_tools_registry() {
  # Seed a persistent tools registry file into .runtime so operators can edit it
  # without rebuilding images.
  # Usage: ns_seed_gateway_tools_registry <repo_root>
  local repo_root="$1"
  # Backwards-compatible wrapper for older scripts/docs.
  ns_seed_gateway_config_files "$repo_root"
}

ns_die() {
  ns_print_error "$1"
  exit 1
}

ns_require_cmd() {
  local cmd="$1"
  local name="${2:-$1}"
  if ! ns_have_cmd "$cmd"; then
    ns_print_error "$name is required but not installed."
    return 1
  fi
  return 0
}

ns_is_tty() { [[ -t 0 && -t 1 ]]; }

ns_detect_platform() {
  local uname_s
  uname_s="$(uname -s 2>/dev/null || echo unknown)"
  case "$uname_s" in
    Darwin) echo "macos" ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null; then
        echo "wsl"
      else
        echo "linux"
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *) echo "unknown" ;;
  esac
}

ns_confirm() {
  local prompt="$1"
  local auto_yes="${NS_AUTO_YES:-false}"
  if [[ "$auto_yes" == "true" ]]; then
    return 0
  fi
  if ! ns_is_tty; then
    return 1
  fi
  read -r -p "$prompt (y/N) " -n 1 REPLY
  echo
  [[ "$REPLY" =~ ^[Yy]$ ]]
}

ns_stat_perms() {
  # Echo numeric perms for a path, or empty if unknown
  local path="$1"
  stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path" 2>/dev/null || true
}

ns_generate_token() {
  if ns_have_cmd openssl; then
    openssl rand -hex 32 2>/dev/null && return 0
  fi
  if ns_have_cmd python3; then
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
    return 0
  fi
  if ns_have_cmd python; then
    python - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
    return 0
  fi
  if ns_have_cmd powershell.exe; then
    powershell.exe -NoProfile -Command "[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')" | tr -d '\r' | head -c 64
    echo
    return 0
  fi
  tr -dc 'a-zA-Z0-9' </dev/urandom | fold -w 64 | head -n 1
}

ns_pick_python() {
  if ns_have_cmd python3; then
    echo "python3"
    return 0
  fi
  if ns_have_cmd python; then
    echo "python"
    return 0
  fi
  echo ""
  return 1
}

ns_read_choice_char() {
  # ns_read_choice_char <prompt> <default_char> <regex>
  # Prints the chosen character to stdout.
  local prompt="$1"
  local default_char="$2"
  local regex="$3"

  local choice=""
  if ns_is_tty; then
    read -r -p "$prompt" -n 1 choice || true
    echo
  fi

  if [[ -z "${choice:-}" ]]; then
    choice="$default_char"
  fi

  if [[ -n "$regex" ]] && [[ ! "$choice" =~ $regex ]]; then
    choice="$default_char"
  fi

  echo "$choice"
}

ns_confirm_default_yes() {
  # Like ns_confirm, but with (Y/n) semantics.
  local prompt="$1"
  local auto_yes="${NS_AUTO_YES:-false}"
  if [[ "$auto_yes" == "true" ]]; then
    return 0
  fi
  if ! ns_is_tty; then
    return 1
  fi
  read -r -p "$prompt (Y/n) " -n 1 REPLY || true
  echo
  if [[ -z "${REPLY:-}" ]]; then
    return 0
  fi
  [[ ! "$REPLY" =~ ^[Nn]$ ]]
}

ns_install_prereqs_linux() {
  local need_docker="$1"; shift
  local need_curl="$1"; shift
  local need_openssl="$1"; shift
  local need_git="$1"; shift
  local need_python="$1"; shift

  local SUDO
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    SUDO=""
  elif ns_have_cmd sudo; then
    SUDO="sudo"
  else
    ns_print_error "Need root privileges (sudo not found)."
    return 1
  fi

  if ns_have_cmd apt-get; then
    $SUDO apt-get update
    local pkgs=()
    [[ "$need_docker" == "true" ]] && pkgs+=(docker.io docker-compose-plugin)
    [[ "$need_curl" == "true" ]] && pkgs+=(curl ca-certificates)
    [[ "$need_openssl" == "true" ]] && pkgs+=(openssl)
    [[ "$need_git" == "true" ]] && pkgs+=(git)
    [[ "$need_python" == "true" ]] && pkgs+=(python3)
    ((${#pkgs[@]})) && $SUDO apt-get install -y "${pkgs[@]}"
    return 0
  fi

  if ns_have_cmd dnf; then
    local pkgs=()
    [[ "$need_docker" == "true" ]] && pkgs+=(docker docker-compose-plugin)
    [[ "$need_curl" == "true" ]] && pkgs+=(curl ca-certificates)
    [[ "$need_openssl" == "true" ]] && pkgs+=(openssl)
    [[ "$need_git" == "true" ]] && pkgs+=(git)
    [[ "$need_python" == "true" ]] && pkgs+=(python3)
    ((${#pkgs[@]})) && $SUDO dnf install -y "${pkgs[@]}"
    return 0
  fi

  if ns_have_cmd yum; then
    local pkgs=()
    [[ "$need_docker" == "true" ]] && pkgs+=(docker docker-compose-plugin)
    [[ "$need_curl" == "true" ]] && pkgs+=(curl ca-certificates)
    [[ "$need_openssl" == "true" ]] && pkgs+=(openssl)
    [[ "$need_git" == "true" ]] && pkgs+=(git)
    [[ "$need_python" == "true" ]] && pkgs+=(python3)
    ((${#pkgs[@]})) && $SUDO yum install -y "${pkgs[@]}"
    return 0
  fi

  if ns_have_cmd pacman; then
    local pkgs=()
    [[ "$need_docker" == "true" ]] && pkgs+=(docker docker-compose)
    [[ "$need_curl" == "true" ]] && pkgs+=(curl ca-certificates)
    [[ "$need_openssl" == "true" ]] && pkgs+=(openssl)
    [[ "$need_git" == "true" ]] && pkgs+=(git)
    [[ "$need_python" == "true" ]] && pkgs+=(python)
    ((${#pkgs[@]})) && $SUDO pacman -Sy --noconfirm "${pkgs[@]}"
    return 0
  fi

  if ns_have_cmd zypper; then
    local pkgs=()
    [[ "$need_docker" == "true" ]] && pkgs+=(docker docker-compose)
    [[ "$need_curl" == "true" ]] && pkgs+=(curl ca-certificates)
    [[ "$need_openssl" == "true" ]] && pkgs+=(openssl)
    [[ "$need_git" == "true" ]] && pkgs+=(git)
    [[ "$need_python" == "true" ]] && pkgs+=(python3)
    ((${#pkgs[@]})) && $SUDO zypper --non-interactive install "${pkgs[@]}"
    return 0
  fi

  ns_print_error "Unsupported Linux distro/package manager. Install prerequisites manually."
  return 1
}

ns_install_prereqs_macos() {
  local need_docker="$1"; shift
  local need_curl="$1"; shift
  local need_openssl="$1"; shift
  local need_git="$1"; shift
  local need_python="$1"; shift

  if ! ns_have_cmd brew; then
    ns_print_error "Homebrew not found. Install Homebrew: https://brew.sh"
    return 1
  fi

  [[ "$need_docker" == "true" ]] && (brew install --cask docker || true)
  [[ "$need_curl" == "true" ]] && (brew install curl || true)
  [[ "$need_openssl" == "true" ]] && (brew install openssl || true)
  [[ "$need_git" == "true" ]] && (brew install git || true)
  [[ "$need_python" == "true" ]] && (brew install python || true)

  if [[ "$need_docker" == "true" ]]; then
    ns_print_warn "If Docker Desktop was just installed, launch it once before using docker."
  fi
}

ns_install_docker_windows() {
  if ns_have_cmd powershell.exe; then
    if powershell.exe -NoProfile -Command "Get-Command winget -ErrorAction SilentlyContinue | Out-Null"; then
      powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements"
      ns_print_warn "Docker Desktop install may require reboot/sign-out. Start Docker Desktop before continuing."
      return 0
    fi
    ns_print_error "winget not found. Install Docker Desktop: https://docs.docker.com/desktop/"
    return 1
  fi
  ns_print_error "powershell.exe not available; cannot auto-install Docker Desktop."
  return 1
}

ns_ensure_prereqs() {
  # ns_ensure_prereqs <need_docker> <need_curl> <need_openssl> <need_git> <need_python> <need_ssh>
  local need_docker="$1"; shift
  local need_curl="$1"; shift
  local need_openssl="$1"; shift
  local need_git="$1"; shift
  local need_python="$1"; shift
  local need_ssh="$1"; shift

  local platform
  platform="$(ns_detect_platform)"

  local missing_any="false"
  [[ "$need_docker" == "true" ]] && ! ns_have_cmd docker && missing_any="true"
  [[ "$need_curl" == "true" ]] && ! ns_have_cmd curl && missing_any="true"
  [[ "$need_openssl" == "true" ]] && ! ns_have_cmd openssl && missing_any="true"
  [[ "$need_git" == "true" ]] && ! ns_have_cmd git && missing_any="true"
  if [[ "$need_python" == "true" ]]; then
    if ! ns_have_cmd python3 && ! ns_have_cmd python; then
      missing_any="true"
    fi
  fi
  [[ "$need_ssh" == "true" ]] && ! ns_have_cmd ssh && missing_any="true"

  if [[ "$missing_any" != "true" ]]; then
    return 0
  fi

  ns_print_header "Installing missing prerequisites"
  echo "Detected platform: $platform"

  case "$platform" in
    linux)
      ns_confirm "Attempt to install missing tools with sudo?" && ns_install_prereqs_linux "$need_docker" "$need_curl" "$need_openssl" "$need_git" "$need_python" || true
      ;;
    macos)
      ns_confirm "Attempt to install missing tools via Homebrew?" && ns_install_prereqs_macos "$need_docker" "$need_curl" "$need_openssl" "$need_git" "$need_python" || true
      ;;
    windows)
      if [[ "$need_docker" == "true" ]] && ! ns_have_cmd docker; then
        ns_confirm "Install Docker Desktop via winget?" && ns_install_docker_windows || true
      fi
      ;;
    wsl)
      if [[ "$need_docker" == "true" ]] && ! ns_have_cmd docker; then
        ns_print_error "Docker not found. In WSL, install Docker Desktop on Windows and enable WSL integration: https://docs.docker.com/desktop/wsl/"
      fi
      ;;
    *)
      ns_print_warn "Unknown OS; cannot auto-install prerequisites."
      ;;
  esac
}

ns_ensure_env_file() {
  # ns_ensure_env_file <env_file> <root_dir>
  local env_file="$1"
  local root_dir="$2"

  if [[ -f "$env_file" ]]; then
    local perms
    perms="$(ns_stat_perms "$env_file")"
    if [[ -n "$perms" && "$perms" -gt 600 ]]; then
      ns_print_error "Insecure permissions on $env_file (expected 600 or tighter)."
      return 1
    fi
    return 0
  fi

  if [[ ! -f "$root_dir/.env.example" ]]; then
    ns_print_error "Missing $root_dir/.env.example; cannot create $env_file"
    return 1
  fi

  mkdir -p "$(dirname "$env_file")" 2>/dev/null || true

  ns_print_warn "Missing $env_file; creating from $root_dir/.env.example"
  cp "$root_dir/.env.example" "$env_file"
  chmod 600 "$env_file" 2>/dev/null || true

  local token
  token="$(ns_generate_token)"
  if [[ -n "$token" ]]; then
    if [[ "${OSTYPE:-}" == "darwin"* ]]; then
      sed -i '' "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$token/" "$env_file" || true
    else
      sed -i "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$token/" "$env_file" || true
    fi
  fi

  ns_print_ok "Created $env_file"
}
