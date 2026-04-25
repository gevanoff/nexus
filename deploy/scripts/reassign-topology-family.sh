#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

TOPOLOGY_FILE="$ROOT_DIR/deploy/topology/production.json"
FAMILY=""
FROM_HOST=""
TO_HOST=""
COMPONENTS_MODE="move"
HOST_ENV_MODE="move"
WRITE_CHANGES="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/reassign-topology-family.sh --family NAME --from HOST --to HOST
                                                 [--topology-file PATH]
                                                 [--components-mode move|ignore]
                                                 [--host-env-mode move|ignore]
                                                 [--write]

Update the tracked topology manifest for a backend family move.

Supported families:
  vllm
  tts
  qwen3-tts

Examples:
  deploy/scripts/reassign-topology-family.sh --family vllm --from ai1 --to ada2 --write
  deploy/scripts/reassign-topology-family.sh --family tts --from ai1 --to ai2 --write
  deploy/scripts/reassign-topology-family.sh --family qwen3-tts --from ai1 --to ai2 --components-mode ignore --write
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topology-file)
      TOPOLOGY_FILE="${2:-}"
      shift 2
      ;;
    --family)
      FAMILY="${2:-}"
      shift 2
      ;;
    --from)
      FROM_HOST="${2:-}"
      shift 2
      ;;
    --to)
      TO_HOST="${2:-}"
      shift 2
      ;;
    --components-mode)
      COMPONENTS_MODE="${2:-}"
      shift 2
      ;;
    --host-env-mode)
      HOST_ENV_MODE="${2:-}"
      shift 2
      ;;
    --write)
      WRITE_CHANGES="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_print_error "Unknown argument: $1"
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "${FAMILY:-}" ]] || ns_die "--family is required"
[[ -n "${FROM_HOST:-}" ]] || ns_die "--from is required"
[[ -n "${TO_HOST:-}" ]] || ns_die "--to is required"

PYTHON="$(ns_pick_python || true)"
[[ -n "${PYTHON:-}" ]] || ns_die "python3/python is required but not installed."

args=(
  move-family
  --topology-file "$TOPOLOGY_FILE"
  --family "$FAMILY"
  --from-host "$FROM_HOST"
  --to-host "$TO_HOST"
  --components-mode "$COMPONENTS_MODE"
  --host-env-mode "$HOST_ENV_MODE"
)

if [[ "$WRITE_CHANGES" == "true" ]]; then
  args+=(--write)
fi

ns_print_header "Reassigning topology family"
"$PYTHON" "$ROOT_DIR/deploy/scripts/topology_tool.py" "${args[@]}"

if [[ "$WRITE_CHANGES" == "true" ]]; then
  cat <<EOF

Next steps:
  1) Deploy destination host first: ./deploy/scripts/ansible-topology.sh deploy ${TO_HOST}
  2) Deploy any gateway host so env URLs refresh: ./deploy/scripts/ansible-topology.sh deploy ai2
  3) Deploy source host last to remove old components: ./deploy/scripts/ansible-topology.sh deploy ${FROM_HOST}
  4) Verify gateway/upstreams: ./deploy/scripts/verify-gateway.sh && ./deploy/scripts/smoke-test-gateway.sh
  5) If registry drift remains, re-register services from the gateway host.
EOF
  if [[ "$FAMILY" == "vllm" ]]; then
    cat <<'EOF'
  6) For any gated or rate-limited vLLM model family, make sure the destination host has the required Hugging Face token before deploy.
EOF
  fi
fi
