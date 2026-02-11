#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

red() { printf '\033[0;31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }

confirm() {
  local prompt="$1"
  local reply
  read -r -p "$prompt [y/N]: " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

need_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1
}

require_cmd() {
  local cmd="$1"
  if ! need_cmd "$cmd"; then
    red "Required command missing: $cmd"
    red "Please install $cmd before running this script."
    exit 1
  fi
}

install_linux_docker() {
  if need_cmd docker; then
    green "Docker already installed: $(docker --version 2>/dev/null || true)"
    return
  fi

  if ! confirm "Docker is not installed. Install Docker Engine using get.docker.com?"; then
    yellow "Skipping Docker installation."
    return
  fi

  if ! need_cmd sudo; then
    red "sudo is required for Docker installation on Linux."
    exit 1
  fi

  local script_path
  script_path="$(mktemp -p /tmp nexus-get-docker.XXXXXX.sh)"
  trap 'rm -f "$script_path"' EXIT

  curl -fsSL https://get.docker.com -o "$script_path"
  sudo sh "$script_path"

  rm -f "$script_path"
  trap - EXIT

  if [[ -n "${SUDO_USER:-}" ]]; then
    sudo usermod -aG docker "$SUDO_USER" || true
  else
    sudo usermod -aG docker "$USER" || true
  fi

  green "Docker installed. You may need to log out and back in for docker group changes to apply."
}

install_linux_compose_plugin() {
  if docker compose version >/dev/null 2>&1; then
    green "Docker Compose plugin already available: $(docker compose version)"
    return
  fi

  if ! confirm "Docker Compose plugin is missing. Install docker-compose-plugin via apt-get?"; then
    yellow "Skipping Docker Compose plugin installation."
    return
  fi

  if ! need_cmd sudo; then
    red "sudo is required for Docker Compose plugin installation on Linux."
    exit 1
  fi

  if ! need_cmd apt-get; then
    yellow "apt-get is not available on this system. Please install the Docker Compose plugin using your distribution's package manager or see: https://docs.docker.com/compose/install/linux/"
    return
  fi
  sudo apt-get update
  sudo apt-get install -y docker-compose-plugin
  green "Docker Compose plugin installation complete."
}

install_macos_docker() {
  if need_cmd docker; then
    green "Docker command detected: $(docker --version 2>/dev/null || true)"
    return
  fi

  if ! need_cmd brew; then
    red "Homebrew is required for scripted macOS Docker Desktop installation. Install Homebrew first."
    exit 1
  fi

  echo
  yellow "macOS note: containers require a Linux VM on macOS."
  yellow "For headless hosts, we recommend Colima (CLI-managed)."
  echo

  if confirm "Install Colima (headless) + docker CLI + docker compose plugin via Homebrew?"; then
    brew install colima docker docker-compose
    green "Colima + docker CLI installed."
    if confirm "Start Colima now (recommended)?"; then
      colima start
      green "Colima started. 'docker info' should work now."
    else
      yellow "Start Colima later with: colima start"
    fi
    return
  fi

  if confirm "Install Docker Desktop with Homebrew cask instead?"; then
    brew install --cask docker
    green "Docker Desktop installed. Start Docker Desktop manually before continuing."
    return
  fi

  yellow "Skipping Docker installation on macOS."
}

install_nvidia_runtime() {
  if ! confirm "Configure NVIDIA Container Toolkit (Linux only, optional)?"; then
    yellow "Skipping NVIDIA runtime setup."
    return
  fi

  if [[ "${OSTYPE:-}" != linux* ]]; then
    yellow "NVIDIA runtime setup is only scripted for Linux."
    return
  fi

  if ! need_cmd sudo || ! need_cmd gpg; then
    red "sudo and gpg are required to configure NVIDIA runtime repositories."
    return
  fi

  if [[ ! -r /etc/os-release ]]; then
    red "Cannot read /etc/os-release; unable to detect Linux distribution."
    return
  fi

  local distro
  distro="$(. /etc/os-release; printf '%s%s' "$ID" "$VERSION_ID")"

  # Check for apt-get availability (required for Debian/Ubuntu-based systems)
  if ! command -v apt-get >/dev/null 2>&1; then
    red "apt-get is not available. This NVIDIA runtime setup only supports apt-based distributions."
    red "Please install NVIDIA Container Toolkit manually for your distribution."
    return
  fi

  curl -fsSL https://nvidia.github.io/nvidia-docker/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL "https://nvidia.github.io/nvidia-docker/${distro}/nvidia-docker.list" \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    | sudo tee /etc/apt/sources.list.d/nvidia-docker.list >/dev/null

  sudo apt-get update
  sudo apt-get install -y nvidia-docker2

  # Restart Docker using systemctl if available, otherwise provide manual instructions
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart docker
  else
    yellow "systemctl is not available. Please restart Docker manually to apply NVIDIA runtime changes."
    yellow "Common alternatives: 'sudo service docker restart' or 'sudo /etc/init.d/docker restart'"
  fi

  if confirm "Run NVIDIA runtime validation container (docker run --gpus all ... nvidia-smi)?"; then
    docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
  fi

  green "NVIDIA runtime setup completed."
}

main() {
  echo "Nexus host dependency installer"
  echo "This script is interactive and may run privileged package installation commands."

  # Validate required commands
  require_cmd curl
  require_cmd mktemp

  if [[ "${OSTYPE:-}" == linux* ]]; then
    install_linux_docker
    install_linux_compose_plugin
  elif [[ "${OSTYPE:-}" == darwin* ]]; then
    install_macos_docker
  else
    yellow "Unsupported OSTYPE '${OSTYPE:-unknown}'. Manual dependency installation may be required."
  fi

  install_nvidia_runtime

  echo
  green "Done. Run './deploy/scripts/preflight-check.sh' to verify host readiness."
}

main "$@"
