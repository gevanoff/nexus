#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

ns_require_cmd shellcheck "shellcheck" || exit 1
ns_require_cmd shfmt "shfmt" || exit 1

INCLUDE_ALL="false"
shell_targets=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      INCLUDE_ALL="true"
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      shell_targets+=("$1")
      shift
      ;;
  esac
done

if [[ ${#shell_targets[@]} -eq 0 && "$INCLUDE_ALL" == "true" ]]; then
  shell_targets=(
    quickstart.sh
  )

  while IFS= read -r path; do
    shell_targets+=("$path")
  done < <(find deploy/scripts services -type f \( -name '*.sh' -o -name 'docker-entrypoint.sh' \) -print | sort)
fi

if [[ ${#shell_targets[@]} -eq 0 ]]; then
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    while IFS= read -r path; do
      [[ -n "$path" ]] || continue
      case "$path" in
        quickstart.sh | deploy/scripts/*.sh | services/*/scripts/*.sh | services/*/docker-entrypoint.sh)
          shell_targets+=("$path")
          ;;
      esac
    done < <(
      {
        git diff --name-only --cached --diff-filter=ACMR
        git diff --name-only --diff-filter=ACMR
        git ls-files --others --exclude-standard
      } | sort -u
    )
  fi
fi

if [[ ${#shell_targets[@]} -eq 0 ]]; then
  ns_print_warn "No shell targets selected. Pass paths explicitly or use --all."
  exit 0
fi

ns_print_header "Shell syntax"
bash -n "${shell_targets[@]}"

ns_print_header "Shell formatting"
shfmt -d -i 2 -ci "${shell_targets[@]}"

ns_print_header "Shell lint"
shellcheck "${shell_targets[@]}"

ns_print_ok "Shell checks passed"
