#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -n "${MLX_HF_CACHE_SOURCE_DIR:-}" ]]; then
  SRC="$MLX_HF_CACHE_SOURCE_DIR"
elif [[ -d "/private/var/lib/mlx/cache/huggingface" ]]; then
  SRC="/private/var/lib/mlx/cache/huggingface"
else
  SRC="/var/lib/mlx/cache/huggingface"
fi

DST="${MLX_HF_CACHE_STATUS_DIR:-$ROOT_DIR/.runtime/gateway/mlx_hf_cache}"

case "$DST" in
  "$ROOT_DIR"/.runtime/gateway/mlx_hf_cache) ;;
  *)
    echo "Refusing to replace unexpected MLX cache status directory: $DST" >&2
    exit 1
    ;;
esac

tmp="${DST}.tmp.$$"
rm -rf "$tmp"
mkdir -p "$tmp"

if [[ -d "$SRC" ]]; then
  for repo in "$SRC"/models--* "$SRC"/hub/models--*; do
    [[ -d "$repo" ]] || continue
    rel="${repo#"$SRC"/}"
    mkdir -p "$tmp/$rel"

    if [[ -d "$repo/blobs" ]]; then
      while IFS= read -r partial; do
        [[ -n "$partial" ]] || continue
        mkdir -p "$tmp/$rel/blobs"
        : >"$tmp/$rel/blobs/$(basename "$partial")"
      done < <(find "$repo/blobs" -maxdepth 1 -name '*.incomplete' -type f -print)
    fi

    if [[ -d "$repo/snapshots" ]]; then
      for snap in "$repo"/snapshots/*; do
        [[ -d "$snap" ]] || continue
        if find "$snap" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
          mkdir -p "$tmp/$rel/snapshots/$(basename "$snap")"
          : >"$tmp/$rel/snapshots/$(basename "$snap")/.cached"
        fi
      done
    fi
  done
else
  echo "MLX Hugging Face cache source not found: $SRC" >&2
fi

rm -rf "$DST"
mv "$tmp" "$DST"

echo "Synced MLX cache status mirror: $SRC -> $DST"
