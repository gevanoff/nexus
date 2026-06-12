#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

TOPOLOGY_FILE="$ROOT_DIR/deploy/topology/production.json"
BATCH_MODE="true"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/topology-ssh-script.sh [--topology-file PATH] [--prompt] <host> [arg ...]

Resolve a tracked topology host to its SSH target and execute a shell script
from stdin via `bash -s -- ...` on the remote host.

Examples:
  ./deploy/scripts/topology-ssh-script.sh ai2 <<'EOS'
  hostname
  docker-compose ps
  EOS

  ./deploy/scripts/topology-ssh-script.sh ai2 foo bar <<'EOS'
  printf 'arg1=%s arg2=%s\n' "$1" "$2"
  EOS
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
    -h | --help | help)
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
[[ -n "$host_name" ]] || {
  usage >&2
  exit 1
}
shift || true

[[ -f "$TOPOLOGY_FILE" ]] || ns_die "Topology file not found: $TOPOLOGY_FILE"

python_bin="$(ns_pick_python || true)"
[[ -n "$python_bin" ]] || ns_die "python3/python is required to resolve topology hosts."
ns_require_cmd ssh "ssh" || exit 1

ssh_target="$($python_bin "$ROOT_DIR/deploy/scripts/topology_tool.py" ssh-target --topology-file "$TOPOLOGY_FILE" --host "$host_name")"
[[ -n "$ssh_target" ]] || ns_die "Failed to resolve SSH target for topology host: $host_name"

ssh_opts=("-o" "StrictHostKeyChecking=accept-new")
if [[ "$BATCH_MODE" == "true" ]]; then
  ssh_opts+=("-o" "BatchMode=yes")
else
  ssh_opts+=("-o" "BatchMode=no")
fi

script_body="$(cat)"
[[ -n "$script_body" ]] || ns_die "No script received on stdin."

ssh "${ssh_opts[@]}" "$ssh_target" bash -s -- "$@" <<<"$script_body"
