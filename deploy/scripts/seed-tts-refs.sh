#!/usr/bin/env bash

# Seed shared TTS reference pool from one or more local directories/files.
# - Copies supported audio files into ./.runtime/tts_refs by default
# - Deduplicates by SHA-256 hash against existing refs
# - Uses safe, deterministic-ish voice IDs derived from filenames

if [ -z "${BASH_VERSION:-}" ]; then
  echo "[FAIL] This script must be run with bash (not sh/PowerShell)."
  echo "[INFO] On Windows, run it from WSL: ./deploy/scripts/seed-tts-refs.sh"
  exit 2
fi

set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

usage() {
  cat <<'EOF'
Usage:
  ./deploy/scripts/seed-tts-refs.sh --source <path> [--source <path> ...] [options]

Required:
  --source PATH          Source directory or audio file. Can be repeated.

Options:
  --target DIR           Destination refs directory (default: ./.runtime/tts_refs)
  --non-recursive        Do not recurse into subdirectories
  --force                On name collision, write with a numeric suffix instead of failing
  --dry-run              Show actions without copying files
  -h, --help             Show this help

Behavior:
  - Supports: wav, mp3, ogg, webm, flac, m4a
  - Creates voice IDs from sanitized filenames
  - Deduplicates by file content hash (SHA-256)
EOF
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

to_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

sha256_file() {
  local path="$1"
  if have_cmd sha256sum; then
    sha256sum "$path" | awk '{print $1}'
    return 0
  fi
  if have_cmd shasum; then
    shasum -a 256 "$path" | awk '{print $1}'
    return 0
  fi
  if have_cmd openssl; then
    openssl dgst -sha256 "$path" | awk '{print $NF}'
    return 0
  fi
  ns_print_error "Need one of: sha256sum, shasum, openssl"
  return 1
}

sanitize_name() {
  local raw="$1"
  local cleaned
  cleaned="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/_/g; s/^[._-]+//; s/[._-]+$//')"
  if [[ -z "${cleaned:-}" ]]; then
    cleaned="voice"
  fi
  printf '%s' "${cleaned:0:48}"
}

is_audio_ext() {
  local ext="$1"
  case "$(to_lower "$ext")" in
    wav|mp3|ogg|webm|flac|m4a) return 0 ;;
    *) return 1 ;;
  esac
}

find_existing_hash() {
  local hash="$1"
  local target="$2"
  local found=""
  while IFS= read -r -d '' file; do
    local h
    h="$(sha256_file "$file")"
    if [[ "$h" == "$hash" ]]; then
      found="$file"
      break
    fi
  done < <(find "$target" -maxdepth 1 -type f \( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.ogg' -o -iname '*.webm' -o -iname '*.flac' -o -iname '*.m4a' \) -print0)
  printf '%s' "$found"
}

TARGET="./.runtime/tts_refs"
RECURSIVE="true"
FORCE="false"
DRY_RUN="false"
declare -a SOURCES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCES+=("${2:-}")
      shift 2 || true
      ;;
    --target)
      TARGET="${2:-$TARGET}"
      shift 2 || true
      ;;
    --non-recursive)
      RECURSIVE="false"
      shift
      ;;
    --force)
      FORCE="true"
      shift
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
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

if [[ ${#SOURCES[@]} -eq 0 ]]; then
  ns_print_error "At least one --source is required"
  usage
  exit 2
fi

mkdir -p "$TARGET"

copied=0
skipped_dupe=0
skipped_invalid=0
failed=0

collect_from_dir() {
  local dir="$1"
  local recurse="$2"
  if [[ "$recurse" == "true" ]]; then
    find "$dir" -type f -print0
  else
    find "$dir" -maxdepth 1 -type f -print0
  fi
}

process_file() {
  local src="$1"
  local filename ext ext_lc stem safe_stem src_hash existing dst candidate idx

  filename="$(basename "$src")"
  ext="${filename##*.}"
  if [[ "$ext" == "$filename" ]]; then
    skipped_invalid=$((skipped_invalid + 1))
    ns_print_warn "Skipping (no extension): $src"
    return 0
  fi
  if ! is_audio_ext "$ext"; then
    skipped_invalid=$((skipped_invalid + 1))
    ns_print_warn "Skipping (unsupported type .$ext): $src"
    return 0
  fi

  stem="${filename%.*}"
  safe_stem="$(sanitize_name "$stem")"
  ext_lc="$(to_lower "$ext")"
  src_hash="$(sha256_file "$src")"

  existing="$(find_existing_hash "$src_hash" "$TARGET")"
  if [[ -n "$existing" ]]; then
    skipped_dupe=$((skipped_dupe + 1))
    ns_print_ok "Duplicate content already present -> $(basename "$existing")"
    return 0
  fi

  dst="$TARGET/${safe_stem}.${ext_lc}"
  if [[ -e "$dst" ]]; then
    if [[ "$FORCE" == "true" ]]; then
      idx=2
      candidate="$TARGET/${safe_stem}_${idx}.${ext_lc}"
      while [[ -e "$candidate" ]]; do
        idx=$((idx + 1))
        candidate="$TARGET/${safe_stem}_${idx}.${ext_lc}"
      done
      dst="$candidate"
    else
      failed=$((failed + 1))
      ns_print_error "Name collision at $dst (use --force to suffix)"
      return 0
    fi
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] copy '$src' -> '$dst'"
    copied=$((copied + 1))
    return 0
  fi

  cp "$src" "$dst"
  copied=$((copied + 1))
  ns_print_ok "Seeded $(basename "$dst")"
}

for source in "${SOURCES[@]}"; do
  if [[ -z "${source:-}" ]]; then
    continue
  fi

  if [[ -d "$source" ]]; then
    while IFS= read -r -d '' f; do
      process_file "$f"
    done < <(collect_from_dir "$source" "$RECURSIVE")
    continue
  fi

  if [[ -f "$source" ]]; then
    process_file "$source"
    continue
  fi

  failed=$((failed + 1))
  ns_print_error "Source not found: $source"
done

echo
ns_print_header "TTS refs seed summary"
echo "Target: $TARGET"
echo "Copied: $copied"
echo "Skipped duplicates: $skipped_dupe"
echo "Skipped invalid: $skipped_invalid"
echo "Errors: $failed"

if [[ "$failed" -gt 0 ]]; then
  exit 1
fi

exit 0
