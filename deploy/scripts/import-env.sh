#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/import-env.sh [--from PATH]

Suggested order (typical):
  1) ./deploy/scripts/install-host-deps.sh
  2) ./deploy/scripts/import-env.sh   (this script)
  3) ./deploy/scripts/preflight-check.sh --mode deploy
  4) ./deploy/scripts/deploy.sh dev main   (or prod)

Creates repo-root .env from .env.example if it doesn't exist.

If a source env file is provided (or auto-detected), merges known keys
(keys present in .env.example) into the new .env. Unknown keys found in
the source file are appended as commented lines, with a note about the
source file.

Options:
  --from PATH   Explicit source env file to import (e.g. /var/lib/gateway/app/.env)
  -h, --help    Show this help
EOF
}

FROM_PATH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      FROM_PATH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ns_print_error "Unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

out_env="$ROOT_DIR/.env"
template_env="$ROOT_DIR/.env.example"

if [[ -f "$out_env" ]]; then
  ns_print_ok ".env already exists; leaving it unchanged: $out_env"
  exit 0
fi

if [[ ! -f "$template_env" ]]; then
  ns_die "Missing .env.example at repo root: $template_env"
fi

find_source_env() {
  # Echo the first suitable source env file path, else empty.
  if [[ -n "${FROM_PATH:-}" ]]; then
    if [[ -f "$FROM_PATH" ]]; then
      echo "$FROM_PATH"
      return 0
    fi
    ns_print_warn "--from path not found: $FROM_PATH"
  fi

  local candidates=()
  candidates+=("/var/lib/gateway/app/.env")

  local services=(gateway nexus ollama mlx images tts etcd observability)
  local svc
  for svc in "${services[@]}"; do
    candidates+=("/etc/${svc}/.env")
    candidates+=("/etc/${svc}/${svc}.env")
    candidates+=("/etc/${svc}/env")
    candidates+=("/etc/${svc}/environment")
    # Common: directory contains multiple env-like files
    candidates+=("/etc/${svc}/"*.env)
  done

  local path
  for path in "${candidates[@]}"; do
    # Keep glob literal if it didn't match anything
    if [[ "$path" == *"*"* && ! -e "$path" ]]; then
      continue
    fi
    [[ -f "$path" ]] || continue
    [[ -r "$path" ]] || continue
    # Heuristic: must contain at least one KEY=VALUE assignment.
    if grep -qE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$path" 2>/dev/null; then
      echo "$path"
      return 0
    fi
  done

  echo ""
  return 0
}

source_env="$(find_source_env)"

ensure_token() {
  local env_path="$1"
  local platform_local="$2"
  local token
  token="$(grep -E '^GATEWAY_BEARER_TOKEN=' "$env_path" | head -n 1 | cut -d '=' -f2- || true)"

  if [[ -z "${token:-}" || "$token" == "change-me-in-production" || "$token" == "your-secret-token-here" ]]; then
    local new_token
    new_token="$(ns_generate_token | tr -d '\r\n')"
    if grep -qE '^GATEWAY_BEARER_TOKEN=' "$env_path"; then
      if [[ "$platform_local" == "macos" ]]; then
        sed -i '' "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$new_token/" "$env_path" || true
      else
        sed -i "s/^GATEWAY_BEARER_TOKEN=.*/GATEWAY_BEARER_TOKEN=$new_token/" "$env_path" || true
      fi
    else
      printf '\nGATEWAY_BEARER_TOKEN=%s\n' "$new_token" >>"$env_path"
    fi
    ns_print_ok "Generated GATEWAY_BEARER_TOKEN in .env"
  fi
}

ns_print_header "Creating .env"
cp "$template_env" "$out_env"
chmod 600 "$out_env" 2>/dev/null || true

if [[ -z "${source_env:-}" ]]; then
  ns_print_warn "No source env file found; created .env from .env.example only"
  platform="$(ns_detect_platform)"
  ensure_token "$out_env" "$platform"
  ns_print_ok "Created: $out_env"
  exit 0
fi

ns_print_header "Importing from source"
ns_print_ok "Source env: $source_env"

platform="$(ns_detect_platform)"

PYTHON="$(ns_pick_python || true)"

if [[ -n "${PYTHON:-}" ]]; then
  counts="$("$PYTHON" - "$template_env" "$source_env" "$out_env" <<'PY'
import os
import re
import sys
from datetime import datetime, timezone

template_path, source_path, out_path = sys.argv[1:4]

key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

def parse_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip("\n")
                if not s.strip() or s.lstrip().startswith("#"):
                    continue
                m = key_re.match(s)
                if not m:
                    continue
                key = m.group(1)
                val = m.group(2)
                values[key] = val
    except FileNotFoundError:
        return {}
    return values

template_lines: list[str] = []
known_keys: set[str] = set()
with open(template_path, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        template_lines.append(line)
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m:
            known_keys.add(m.group(1))

src_values = parse_env(source_path)

updated = 0
out_lines: list[str] = []
for line in template_lines:
    m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
    if not m:
        out_lines.append(line)
        continue
    prefix_ws, key, _rest = m.group(1), m.group(2), m.group(3)
    if key in src_values:
        out_lines.append(f"{prefix_ws}{key}={src_values[key]}\n")
        updated += 1
    else:
        out_lines.append(line)

unknown = {k: v for k, v in src_values.items() if k not in known_keys}

stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

if unknown:
    out_lines.append("\n")
    out_lines.append(f"# --- Imported from {source_path} ({stamp}) ---\n")
    out_lines.append("# Unknown keys from source (commented out):\n")
    for key in sorted(unknown.keys()):
        out_lines.append(f"# {key}={unknown[key]}\n")

with open(out_path, "w", encoding="utf-8", newline="\n") as f:
    f.writelines(out_lines)

print(f"{updated} {len(unknown)}")
PY
)"
  updated_count="${counts%% *}"
  unknown_count="${counts##* }"
  ns_print_ok "Merged ${updated_count:-0} known key(s); appended ${unknown_count:-0} unknown key(s) as comments"
else
  ns_print_warn "python3/python not found; falling back to basic shell import"

  tmp_known="$(mktemp -p /tmp nexus-known-keys.XXXXXX 2>/dev/null || mktemp)"
  trap 'rm -f "$tmp_known"' EXIT

  awk -F= '/^[A-Za-z_][A-Za-z0-9_]*=/{print $1}' "$template_env" | sort -u >"$tmp_known"

  # Replace known keys in-place.
  while IFS= read -r key; do
    [[ -n "${key:-}" ]] || continue
    src_line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$source_env" | head -n 1 || true)"
    [[ -n "${src_line:-}" ]] || continue
    src_line="${src_line#export }"
    value="${src_line#*=}"
    # Escape for sed replacement: backslash, ampersand, and delimiter.
    esc_value="${value//\\/\\\\}"
    esc_value="${esc_value//&/\\&}"
    esc_value="${esc_value//\//\\/}"
    if [[ "$platform" == "macos" ]]; then
      sed -i '' "s/^${key}=.*/${key}=${esc_value}/" "$out_env" || true
    else
      sed -i "s/^${key}=.*/${key}=${esc_value}/" "$out_env" || true
    fi
  done <"$tmp_known"

  # Append unknown keys as commented lines.
  unknown_tmp="$(mktemp -p /tmp nexus-unknown-keys.XXXXXX 2>/dev/null || mktemp)"
  awk -v known_file="$tmp_known" '
    BEGIN { while ((getline < known_file) > 0) { known[$0] = 1 } }
    /^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=/ {
      line = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      split(line, a, "=")
      k = a[1]
      if (!(k in known)) print line
    }
  ' "$source_env" | sort -u >"$unknown_tmp"

  if [[ -s "$unknown_tmp" ]]; then
    {
      echo
      echo "# --- Imported from ${source_env} ($(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)) ---"
      echo "# Unknown keys from source (commented out):"
      while IFS= read -r line; do
        echo "# ${line}"
      done <"$unknown_tmp"
    } >>"$out_env"
  fi

  rm -f "$unknown_tmp" || true
  trap - EXIT
fi

ensure_token "$out_env" "$platform"
chmod 600 "$out_env" 2>/dev/null || true

ns_print_ok "Created: $out_env"
