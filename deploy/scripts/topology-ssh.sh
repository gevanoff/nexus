#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

TOPOLOGY_FILE="$ROOT_DIR/deploy/topology/production.json"
BATCH_MODE="true"
PRINT_TARGET="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/topology-ssh.sh [--topology-file PATH] [--prompt] [--print-target] <host> [command...]

Resolve a tracked topology host to its SSH target, then open SSH or run a remote command.

Options:
  --topology-file PATH  Override the topology manifest (default: deploy/topology/production.json)
  --prompt              Use BatchMode=no for interactive/password-backed SSH
  --print-target        Only print the resolved SSH target

Examples:
  ./deploy/scripts/topology-ssh.sh ai1
  ./deploy/scripts/topology-ssh.sh ai2 docker ps
  ./deploy/scripts/topology-ssh.sh --print-target ada2
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topology-file)
      TOPOLOGY_FILE="${2:-}"
      shift 2
      ;;
    --prompt)
      BATCH_MODE="false"
      shift
      ;;
    --print-target)
      PRINT_TARGET="true"
      shift
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      ns_print_error "Unknown option: $1"
      usage >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

host_name="${1:-}"
if [[ -z "${host_name:-}" ]]; then
  usage >&2
  exit 1
fi
shift || true

if [[ ! -f "$TOPOLOGY_FILE" ]]; then
  ns_print_error "Topology file not found: $TOPOLOGY_FILE"
  exit 1
fi

python_bin="$(ns_pick_python || true)"
if [[ -z "${python_bin:-}" ]]; then
  ns_print_error "python3/python is required to resolve topology hosts."
  exit 1
fi
if ! ns_have_cmd ssh; then
  ns_print_error "ssh is required but not installed."
  exit 1
fi

ssh_target="$("$python_bin" "$ROOT_DIR/deploy/scripts/topology_tool.py" ssh-target \
  --topology-file "$TOPOLOGY_FILE" \
  --host "$host_name")"

if [[ -z "${ssh_target:-}" ]]; then
  ns_print_error "Failed to resolve SSH target for topology host: $host_name"
  exit 1
fi

if [[ "$PRINT_TARGET" == "true" ]]; then
  echo "$ssh_target"
  exit 0
fi

ssh_opts=("-o" "StrictHostKeyChecking=accept-new")
if [[ "$BATCH_MODE" == "true" ]]; then
  ssh_opts+=("-o" "BatchMode=yes")
else
  ssh_opts+=("-o" "BatchMode=no")
fi

remote_env_prefix='export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-};'

if [[ $# -eq 0 ]]; then
  exec ssh "${ssh_opts[@]}" -t "$ssh_target" "${remote_env_prefix} exec \${SHELL:-/bin/bash} -l"
fi

if [[ $# -eq 1 ]]; then
  remote_command="$1"
else
  printf -v remote_command '%q ' "$@"
  remote_command="${remote_command% }"
fi

printf -v remote_shell_command '%s exec /bin/bash -lc %q' "$remote_env_prefix" "$remote_command"
exec ssh "${ssh_opts[@]}" "$ssh_target" "$remote_shell_command"
