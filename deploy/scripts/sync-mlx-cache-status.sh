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
        marker="$tmp/$rel/blobs/$(basename "$partial")"
        : >"$marker"
        touch -r "$partial" "$marker" 2>/dev/null || true
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

if command -v python3 >/dev/null 2>&1; then
  SRC="$SRC" DST_TMP="$tmp" python3 - <<'PY'
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def repo_id_from_cache_dir(path: Path) -> str:
    name = path.name
    if not name.startswith("models--"):
        return ""
    return name.removeprefix("models--").replace("--", "/").strip()


src = Path(os.environ["SRC"])
dst = Path(os.environ["DST_TMP"])
models: dict[str, dict[str, object]] = {}
try:
    stalled_after_sec = float(os.environ.get("MLX_FETCH_STALLED_AFTER_SEC", "600") or "600")
except ValueError:
    stalled_after_sec = 600.0
now = time.time()

if src.exists():
    candidates: list[Path] = []
    for base in (src, src / "hub"):
        if base.exists():
            candidates.extend(path for path in base.iterdir() if path.is_dir() and path.name.startswith("models--"))

    for repo in candidates:
        model = repo_id_from_cache_dir(repo)
        if not model:
            continue
        entry = models.setdefault(
            model,
            {
                "state": "missing",
                "repo_paths": [],
                "incomplete_count": 0,
                "incomplete_bytes": 0,
                "oldest_incomplete_mtime": 0.0,
                "newest_incomplete_mtime": 0.0,
                "snapshot_count": 0,
                "newest_snapshot_mtime": 0.0,
            },
        )
        entry["repo_paths"].append(str(repo))

        incomplete_paths = list((repo / "blobs").glob("*.incomplete")) if (repo / "blobs").exists() else []
        mtimes: list[float] = []
        total_bytes = 0
        for partial in incomplete_paths:
            try:
                stat = partial.stat()
            except OSError:
                continue
            mtimes.append(float(stat.st_mtime))
            total_bytes += int(stat.st_size)
        if mtimes:
            entry["incomplete_count"] = int(entry["incomplete_count"]) + len(mtimes)
            entry["incomplete_bytes"] = int(entry["incomplete_bytes"]) + total_bytes
            oldest = min(mtimes)
            newest = max(mtimes)
            previous_oldest = float(entry["oldest_incomplete_mtime"] or 0.0)
            previous_newest = float(entry["newest_incomplete_mtime"] or 0.0)
            entry["oldest_incomplete_mtime"] = oldest if previous_oldest <= 0 else min(previous_oldest, oldest)
            entry["newest_incomplete_mtime"] = max(previous_newest, newest)

        snapshots = repo / "snapshots"
        if snapshots.exists():
            for snap in snapshots.iterdir():
                if not snap.is_dir():
                    continue
                try:
                    has_files = any(snap.iterdir())
                    mtime = float(snap.stat().st_mtime)
                except OSError:
                    continue
                if has_files:
                    entry["snapshot_count"] = int(entry["snapshot_count"]) + 1
                    entry["newest_snapshot_mtime"] = max(float(entry["newest_snapshot_mtime"] or 0.0), mtime)

        incomplete_count = int(entry["incomplete_count"] or 0)
        snapshot_count = int(entry["snapshot_count"] or 0)
        newest_incomplete = float(entry["newest_incomplete_mtime"] or 0.0)
        incomplete_active = incomplete_count > 0 and newest_incomplete > 0 and (now - newest_incomplete) <= stalled_after_sec
        if incomplete_count > 0 and (snapshot_count <= 0 or incomplete_active):
            entry["state"] = "fetching"
        elif snapshot_count > 0:
            entry["state"] = "cached"

payload = {
    "generated_at": time.time(),
    "source": str(src),
    "models": models,
}
(dst / ".nexus_cache_status.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
PY
fi

find "$tmp" -type d -exec chmod 755 {} +
find "$tmp" -type f -exec chmod 644 {} +

rm -rf "$DST"
mv "$tmp" "$DST"

echo "Synced MLX cache status mirror: $SRC -> $DST"
