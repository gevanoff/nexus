#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# shellcheck source=/dev/null
source "$ROOT_DIR/deploy/scripts/_common.sh"

if [[ -n "${MLX_HF_CACHE_SOURCE_DIR:-}" ]]; then
  SRC="$MLX_HF_CACHE_SOURCE_DIR"
elif [[ -n "${HF_HOME:-}" ]]; then
  SRC="$HF_HOME"
elif [[ -d "/ai-data/huggingface" ]]; then
  SRC="/ai-data/huggingface"
elif [[ -d "/Volumes/ai_data/huggingface" ]]; then
  SRC="/Volumes/ai_data/huggingface"
elif [[ -d "/private/var/lib/huggingface" ]]; then
  SRC="/private/var/lib/huggingface"
else
  SRC="/var/lib/huggingface"
fi

DST="${MLX_HF_CACHE_STATUS_DIR:-$(ns_runtime_root "$ROOT_DIR")/gateway/mlx_hf_cache}"

case "$DST" in
  "$(ns_runtime_root "$ROOT_DIR")"/gateway/mlx_hf_cache) ;;
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
import re
import time
from pathlib import Path


def repo_id_from_cache_dir(path: Path) -> str:
    name = path.name
    if not name.startswith("models--"):
        return ""
    return name.removeprefix("models--").replace("--", "/").strip()


SHARDED_WEIGHT_RE = re.compile(r"^(.+)-(\d{5})-of-(\d{5})\.(safetensors|bin)$")
DOWNLOAD_JOB_FILE = ".nexus_download_status.json"
DOWNLOAD_JOB_FIELDS = {
    "version",
    "model",
    "state",
    "pid",
    "started_at",
    "updated_at",
    "last_progress_at",
    "completed_at",
    "failed_at",
    "attempt",
    "max_attempts",
    "retry_count",
    "revision",
    "downloaded_shards",
    "expected_shards",
    "incomplete_bytes",
    "next_retry_at",
    "error",
}


def download_job_status(repo: Path, model: str) -> dict[str, object]:
    try:
        payload = json.loads((repo / DOWNLOAD_JOB_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict) or str(payload.get("model") or "") != model:
        return {}
    status = {key: value for key, value in payload.items() if key in DOWNLOAD_JOB_FIELDS}
    if "error" in status:
        status["error"] = str(status["error"] or "")[:1000]
    return status


def snapshot_weight_status(snapshot: Path) -> tuple[str, int, int, list[str]]:
    index_path = snapshot / "model.safetensors.index.json"
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        weight_map = payload.get("weight_map") if isinstance(payload, dict) else None
        if isinstance(weight_map, dict) and weight_map:
            required = sorted(
                {
                    str(rel).strip()
                    for rel in weight_map.values()
                    if isinstance(rel, str) and str(rel).strip()
                }
            )
            missing = [rel for rel in required if not (snapshot / rel).exists()]
            return ("complete" if not missing else "incomplete", len(required), len(required) - len(missing), missing[:20])

    weight_files = [
        path
        for pattern in ("*.safetensors", "*.bin")
        for path in snapshot.glob(pattern)
        if path.is_file()
    ]
    if not weight_files:
        return "unknown", 0, 0, []

    expected_by_group: dict[tuple[str, str], int] = {}
    observed_by_group: dict[tuple[str, str], set[int]] = {}
    for path in weight_files:
        match = SHARDED_WEIGHT_RE.match(path.name)
        if not match:
            continue
        group = (match.group(1), match.group(4))
        shard_number = int(match.group(2))
        shard_total = int(match.group(3))
        expected_by_group[group] = max(expected_by_group.get(group, 0), shard_total)
        observed_by_group.setdefault(group, set()).add(shard_number)

    if not expected_by_group:
        return "complete", len(weight_files), len(weight_files), []

    expected_total = 0
    observed_total = 0
    missing: list[str] = []
    for (prefix, suffix), expected in sorted(expected_by_group.items()):
        observed = observed_by_group.get((prefix, suffix), set())
        expected_total += expected
        observed_total += len(observed)
        width = 5
        for number in range(1, expected + 1):
            if number not in observed:
                missing.append(f"{prefix}-{number:0{width}d}-of-{expected:0{width}d}.{suffix}")
                if len(missing) >= 20:
                    break
    return ("complete" if observed_total >= expected_total and not missing else "incomplete", expected_total, observed_total, missing[:20])


src = Path(os.environ["SRC"])
dst = Path(os.environ["DST_TMP"])
models: dict[str, dict[str, object]] = {}

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
                "complete_snapshot_count": 0,
                "incomplete_snapshot_count": 0,
                "safetensors_count": 0,
                "expected_safetensors_count": 0,
                "missing_weight_files": [],
                "newest_snapshot_mtime": 0.0,
                "download_job": {},
            },
        )
        entry["repo_paths"].append(str(repo))

        download_job = download_job_status(repo, model)
        current_job = entry.get("download_job")
        current_updated = float(current_job.get("updated_at") or 0.0) if isinstance(current_job, dict) else 0.0
        if download_job and float(download_job.get("updated_at") or 0.0) >= current_updated:
            entry["download_job"] = download_job

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
                    status, expected_count, observed_count, missing_files = snapshot_weight_status(snap)
                    entry["expected_safetensors_count"] = max(int(entry["expected_safetensors_count"] or 0), expected_count)
                    entry["safetensors_count"] = max(int(entry["safetensors_count"] or 0), observed_count)
                    if status == "complete":
                        entry["complete_snapshot_count"] = int(entry["complete_snapshot_count"] or 0) + 1
                    elif status == "incomplete":
                        entry["incomplete_snapshot_count"] = int(entry["incomplete_snapshot_count"] or 0) + 1
                        current_missing = entry["missing_weight_files"]
                        if not isinstance(current_missing, list):
                            current_missing = []
                        entry["missing_weight_files"] = [*current_missing, *missing_files][:20]

        incomplete_count = int(entry["incomplete_count"] or 0)
        snapshot_count = int(entry["snapshot_count"] or 0)
        complete_snapshot_count = int(entry["complete_snapshot_count"] or 0)
        incomplete_snapshot_count = int(entry["incomplete_snapshot_count"] or 0)
        job = entry.get("download_job")
        job_state = str(job.get("state") or "") if isinstance(job, dict) else ""
        if complete_snapshot_count > 0:
            entry["state"] = "cached"
        elif job_state in {"starting", "downloading", "retry_wait"}:
            entry["state"] = "fetching"
        elif job_state == "failed":
            entry["state"] = "missing"
        elif incomplete_count > 0:
            entry["state"] = "fetching"
        elif incomplete_snapshot_count > 0:
            entry["state"] = "missing"
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

mkdir -p "$DST"
find "$DST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -Rp "$tmp"/. "$DST"/
rm -rf "$tmp"

echo "Synced MLX cache status mirror: $SRC -> $DST"
