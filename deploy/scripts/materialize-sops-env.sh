#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

environment=""
topology_host=""
env_file=""
repo_root="$ROOT_DIR"
print_output_paths="false"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/materialize-sops-env.sh --environment <dev|prod>
                                             [--topology-host <host>]
                                             [--env-file PATH]
                                             [--repo-root PATH]
                                             [--print-output-paths]

Materialize tracked SOPS-encrypted dotenv overlays into generated git-ignored
files next to the selected env file.

Secret source discovery:
  deploy/secrets/<environment>/common.env.sops
  deploy/secrets/<environment>/<topology-host>.env.sops
  deploy/secrets/<environment>/default.env.sops   (when --topology-host is omitted)

Generated overlays:
  <env-file>.sops.common.local
  <env-file>.sops.local
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment)
      environment="${2:-}"
      shift 2
      ;;
    --topology-host)
      topology_host="${2:-}"
      shift 2
      ;;
    --env-file)
      env_file="${2:-}"
      shift 2
      ;;
    --repo-root)
      repo_root="${2:-}"
      shift 2
      ;;
    --print-output-paths)
      print_output_paths="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_print_error "Unknown option: $1"
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${environment:-}" ]]; then
  ns_print_error "--environment is required."
  usage >&2
  exit 1
fi

if [[ -z "${env_file:-}" ]]; then
  env_file="${repo_root}/deploy/env/.env.${environment}"
  if [[ -n "${topology_host:-}" ]]; then
    env_file="${env_file}.${topology_host}"
  fi
fi

ns_prepare_sops_env_overlays "$repo_root" "$environment" "$env_file" "${topology_host:-}"

if [[ "$print_output_paths" == "true" ]]; then
  common_output="$(ns_sops_generated_common_overlay "$env_file")"
  specific_output="$(ns_sops_generated_specific_overlay "$env_file")"
  [[ -f "$common_output" ]] && printf '%s\n' "$common_output"
  [[ -f "$specific_output" ]] && printf '%s\n' "$specific_output"
fi
