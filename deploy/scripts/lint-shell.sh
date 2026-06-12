#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd shellcheck "shellcheck" || exit 1
ns_require_cmd shfmt "shfmt" || exit 1

shell_targets=(
  quickstart.sh
)

while IFS= read -r path; do
  shell_targets+=("$path")
done < <(find deploy/scripts services -type f \( -name '*.sh' -o -name 'docker-entrypoint.sh' \) -print | sort)

if [[ ${#shell_targets[@]} -eq 0 ]]; then
  ns_print_warn "No shell targets found."
  exit 0
fi

ns_print_header "Shell syntax"
bash -n "${shell_targets[@]}"

ns_print_header "Shell formatting"
shfmt -d -i 2 -ci "${shell_targets[@]}"

ns_print_header "Shell lint"
shellcheck "${shell_targets[@]}"

ns_print_ok "Shell checks passed"
