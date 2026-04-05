#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ANSIBLE_DIR="$ROOT_DIR/ansible"
ANSIBLE_CONFIG_PATH="$ANSIBLE_DIR/ansible.cfg"
INVENTORY_PATH="$ANSIBLE_DIR/inventory/topology_inventory.py"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/ansible-topology.sh <inventory|bootstrap|deploy|site> [host|all] [-- extra ansible args]

Convenience wrapper for the Nexus topology-backed Ansible control layer.

Commands:
  inventory   Run ansible-inventory against the topology inventory. Defaults to --graph.
  bootstrap   Run ansible/playbooks/bootstrap.yml, optionally limited to one topology host.
  deploy      Run ansible/playbooks/deploy.yml, optionally limited to one topology host.
  site        Run ansible/playbooks/site.yml, optionally limited to one topology host.

Host:
  ai1 | ai2 | ada2 | all
  Omit the host (or use all) to target the full topology.

Examples:
  ./deploy/scripts/ansible-topology.sh inventory
  ./deploy/scripts/ansible-topology.sh inventory -- --host ai2
  ./deploy/scripts/ansible-topology.sh bootstrap ai1 -- --check
  ./deploy/scripts/ansible-topology.sh deploy ada2
  ./deploy/scripts/ansible-topology.sh site all -- -e nexus_environment=prod
EOF
}

command_name="${1:-}"
if [[ -z "${command_name:-}" ]]; then
  usage >&2
  exit 1
fi
shift || true

case "$command_name" in
  inventory)
    if ! ns_have_cmd ansible-inventory; then
      ns_print_error "ansible-inventory is required but not installed."
      exit 1
    fi
    if [[ "${1:-}" == "--" ]]; then
      shift
    fi
    if [[ $# -eq 0 ]]; then
      set -- --graph
    fi
    ANSIBLE_CONFIG="$ANSIBLE_CONFIG_PATH" ansible-inventory -i "$INVENTORY_PATH" "$@"
    ;;
  bootstrap|deploy|site)
    if ! ns_have_cmd ansible-playbook; then
      ns_print_error "ansible-playbook is required but not installed."
      exit 1
    fi

    host_limit=""
    if [[ $# -gt 0 && "${1:-}" != "--" && "${1:0:1}" != "-" ]]; then
      host_limit="$1"
      shift || true
    fi

    case "${host_limit:-}" in
      ""|all)
        host_limit=""
        ;;
      ai1|ai2|ada2)
        ;;
      *)
        ns_print_error "Unknown topology host: ${host_limit}"
        usage >&2
        exit 2
        ;;
    esac

    if [[ "${1:-}" == "--" ]]; then
      shift
    fi

    playbook_path="$ANSIBLE_DIR/playbooks/${command_name}.yml"
    cmd=(ansible-playbook -i "$INVENTORY_PATH" "$playbook_path")
    if [[ -n "${host_limit:-}" ]]; then
      cmd+=(-l "$host_limit")
    fi
    cmd+=("$@")

    ANSIBLE_CONFIG="$ANSIBLE_CONFIG_PATH" "${cmd[@]}"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    ns_print_error "Unknown command: ${command_name}"
    usage >&2
    exit 2
    ;;
esac
