#!/usr/bin/env bash
# Shared Python interpreter selection/version helpers for Nexus scripts.
# Shellcheck-friendly: this file is sourced, not executed.
# IMPORTANT: Do not modify shell options here.

ns_python_version_major_minor() {
  local py_bin="${1:-}"
  if [[ -z "$py_bin" ]]; then
    return 1
  fi
  "$py_bin" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null
}

ns_python_is_at_least() {
  # Usage: ns_python_is_at_least <python_bin> <min_major> <min_minor>
  local py_bin="${1:-}"
  local min_major="${2:-}"
  local min_minor="${3:-}"
  local ver
  local major
  local minor

  if [[ -z "$py_bin" || -z "$min_major" || -z "$min_minor" ]]; then
    return 1
  fi

  ver="$(ns_python_version_major_minor "$py_bin" || true)"
  if [[ -z "$ver" ]]; then
    return 1
  fi

  major="${ver%%.*}"
  minor="${ver##*.}"

  if [[ "$major" -gt "$min_major" ]]; then
    return 0
  fi
  if [[ "$major" -eq "$min_major" && "$minor" -ge "$min_minor" ]]; then
    return 0
  fi
  return 1
}

ns_python_choose_at_least() {
  # Usage: ns_python_choose_at_least <min_major> <min_minor> [override] [candidates...]
  local min_major="${1:-}"
  local min_minor="${2:-}"
  local override="${3:-}"
  shift 3 || true

  local candidates=("$@")
  local candidate
  local resolved

  if [[ -n "$override" ]]; then
    candidates=("$override" "${candidates[@]}")
  fi

  for candidate in "${candidates[@]}"; do
    [[ -z "$candidate" ]] && continue
    if [[ -x "$candidate" ]]; then
      resolved="$candidate"
    else
      resolved="$(command -v "$candidate" 2>/dev/null || true)"
    fi
    if [[ -n "$resolved" ]] && ns_python_is_at_least "$resolved" "$min_major" "$min_minor"; then
      echo "$resolved"
      return 0
    fi
  done

  return 1
}
