from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from app.backends import backend_provider_name
from app.config import S


def _hf_repo_cache_name(model_id: str) -> str:
    safe_id = (model_id or "").strip().replace("/", "--")
    return f"models--{safe_id}"


def _hf_repo_cache_dirs(model_id: str, cache_dir: str) -> list[Path]:
    name = _hf_repo_cache_name(model_id)
    root = Path(cache_dir)
    return [root / "hub" / name, root / name]


def _hf_cache_root(cache_dir: str | None = None) -> Path:
    return Path((cache_dir or getattr(S, "MLX_HF_CACHE_DIR", "") or "").strip())


def _hf_cache_source_root() -> Path:
    source_dir = (getattr(S, "MLX_HF_CACHE_SOURCE_DIR", "") or "").strip()
    if source_dir:
        return Path(source_dir)
    hf_home = (getattr(S, "HF_HOME", "") or "").strip()
    if hf_home:
        return Path(hf_home)
    for candidate in (
        Path("/ai-data/huggingface"),
        Path("/Volumes/ai_data/huggingface"),
        Path("/private/var/lib/huggingface"),
        Path("/var/lib/huggingface"),
    ):
        if candidate.exists():
            return candidate
    return Path("/var/lib/huggingface")


_SHARDED_WEIGHT_RE = re.compile(r"^(.+)-(\d{5})-of-(\d{5})\.(safetensors|bin)$")


def _snapshot_weight_completeness(snapshot: Path) -> Optional[bool]:
    """Return whether a Hugging Face snapshot appears to have complete weights."""

    index_path = snapshot / "model.safetensors.index.json"
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        weight_map = payload.get("weight_map") if isinstance(payload, dict) else None
        if isinstance(weight_map, dict) and weight_map:
            required = {
                str(rel).strip()
                for rel in weight_map.values()
                if isinstance(rel, str) and str(rel).strip()
            }
            if required:
                return all((snapshot / rel).exists() for rel in required)

    weight_files = [
        path
        for pattern in ("*.safetensors", "*.bin")
        for path in snapshot.glob(pattern)
        if path.is_file()
    ]
    if not weight_files:
        return None

    expected_by_group: dict[tuple[str, str], int] = {}
    observed_by_group: dict[tuple[str, str], set[int]] = {}
    for path in weight_files:
        match = _SHARDED_WEIGHT_RE.match(path.name)
        if not match:
            continue
        group = (match.group(1), match.group(4))
        shard_number = int(match.group(2))
        shard_total = int(match.group(3))
        expected_by_group[group] = max(expected_by_group.get(group, 0), shard_total)
        observed_by_group.setdefault(group, set()).add(shard_number)

    for group, expected in expected_by_group.items():
        observed = observed_by_group.get(group, set())
        if len(observed) < expected or any(number not in observed for number in range(1, expected + 1)):
            return False
    return True


def _cache_status_payload(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / ".nexus_cache_status.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _model_status_metadata(model_id: str, root: Path) -> dict[str, Any]:
    payload = _cache_status_payload(root)
    models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
    entry = models.get(model_id) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        return {}
    out = dict(entry)
    generated_at = payload.get("generated_at")
    if isinstance(generated_at, (int, float)):
        out["status_generated_at"] = float(generated_at)
    return out


def hf_model_cache_state(model_id: str, cache_dir: str | None = None) -> Optional[str]:
    """Return cached/missing/fetching/unknown for a Hugging Face model cache.

    ``None`` means the gateway cannot inspect that cache, usually because the
    host-native cache is not mounted into the container.
    """

    model = (model_id or "").strip()
    root = _hf_cache_root(cache_dir)
    if not model or not root.exists():
        return None

    metadata = _model_status_metadata(model, root)
    metadata_state = str(metadata.get("state") or "").strip()
    if metadata_state in {"cached", "fetching", "missing"}:
        complete_snapshots = _int_metadata(metadata.get("complete_snapshot_count"))
        if _int_metadata(metadata.get("incomplete_count")) > 0:
            if metadata_state == "cached" and complete_snapshots > 0:
                return "cached"
            return "fetching"
        if metadata_state == "cached":
            incomplete_snapshots = _int_metadata(metadata.get("incomplete_snapshot_count"))
            if incomplete_snapshots > 0 and complete_snapshots <= 0:
                return "missing"
        return metadata_state

    repos = [repo for repo in _hf_repo_cache_dirs(model, str(root)) if repo.exists()]
    if not repos:
        return "missing"

    has_incomplete_blob = False
    complete_snapshot_found = False
    incomplete_snapshot_found = False
    any_snapshot_files = False
    for repo in repos:
        if any(repo.glob("blobs/*.incomplete")):
            has_incomplete_blob = True
        snapshots = repo / "snapshots"
        try:
            snapshot_dirs = [path for path in snapshots.iterdir() if path.is_dir()]
        except Exception:
            return None
        for snapshot in snapshot_dirs:
            try:
                has_files = any(snapshot.iterdir())
            except Exception:
                continue
            if not has_files:
                continue
            any_snapshot_files = True
            complete = _snapshot_weight_completeness(snapshot)
            if complete is True:
                complete_snapshot_found = True
            elif complete is False:
                incomplete_snapshot_found = True
    if complete_snapshot_found:
        return "cached"
    if has_incomplete_blob:
        return "fetching"
    if incomplete_snapshot_found:
        return "missing"
    if any_snapshot_files:
        return "cached"
    return "missing"


def _float_metadata(value: Any) -> float:
    try:
        parsed = float(value)
    except Exception:
        return 0.0
    return parsed if parsed > 0 else 0.0


def _int_metadata(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return parsed if parsed > 0 else 0


def _fallback_incomplete_metadata(model: str, root: Path) -> dict[str, Any]:
    newest = 0.0
    oldest = 0.0
    total_bytes = 0
    count = 0
    for repo in (repo for repo in _hf_repo_cache_dirs(model, str(root)) if repo.exists()):
        for partial in repo.glob("blobs/*.incomplete"):
            try:
                stat = partial.stat()
            except OSError:
                continue
            mtime = float(stat.st_mtime)
            newest = max(newest, mtime)
            oldest = mtime if oldest <= 0 else min(oldest, mtime)
            total_bytes += int(stat.st_size)
            count += 1
    return {
        "incomplete_count": count,
        "incomplete_bytes": total_bytes,
        "oldest_incomplete_mtime": oldest,
        "newest_incomplete_mtime": newest,
    }


def _fetch_activity_from_metadata(
    model: str,
    state: Optional[str],
    root: Path,
    metadata: dict[str, Any],
    *,
    stalled_after_sec: float,
    now: float,
) -> Optional[dict[str, Any]]:
    job = metadata.get("download_job") if isinstance(metadata.get("download_job"), dict) else {}
    job_state = str(job.get("state") or "").strip()
    if state != "fetching" and job_state not in {"failed", "complete"}:
        return None

    if job_state:
        updated_at = _float_metadata(job.get("updated_at"))
        last_progress_at = _float_metadata(job.get("last_progress_at"))
        heartbeat_age = max(0.0, now - updated_at) if updated_at > 0 else 0.0
        last_progress_age = max(0.0, now - last_progress_at) if last_progress_at > 0 else 0.0
        if job_state == "failed":
            activity_status = "failed"
            label = "download failed"
        elif job_state == "complete":
            activity_status = "complete"
            label = "download complete"
        elif updated_at <= 0:
            activity_status = "unknown"
            label = "fetch state unknown"
        elif heartbeat_age > stalled_after_sec:
            activity_status = "stalled"
            label = "download worker stopped"
        else:
            activity_status = "active"
            label = "waiting to retry" if job_state == "retry_wait" else "actively downloading"

        downloaded_shards = _int_metadata(job.get("downloaded_shards"))
        expected_shards = _int_metadata(job.get("expected_shards"))
        progress_percent = (
            min(100.0, (downloaded_shards / expected_shards) * 100.0)
            if expected_shards > 0
            else None
        )
        return {
            "status": activity_status,
            "label": label,
            "job_state": job_state,
            "pid": _int_metadata(job.get("pid")),
            "attempt": _int_metadata(job.get("attempt")),
            "max_attempts": _int_metadata(job.get("max_attempts")),
            "retry_count": _int_metadata(job.get("retry_count")),
            "next_retry_at": _float_metadata(job.get("next_retry_at")),
            "downloaded_shards": downloaded_shards,
            "expected_shards": expected_shards,
            "progress_percent": progress_percent,
            "incomplete_bytes": _int_metadata(job.get("incomplete_bytes")),
            "last_progress_at": last_progress_at,
            "last_progress_age_sec": last_progress_age,
            "heartbeat_at": updated_at,
            "heartbeat_age_sec": heartbeat_age,
            "stalled_after_sec": stalled_after_sec,
            "error": str(job.get("error") or "")[:1000],
            "status_generated_at": _float_metadata(metadata.get("status_generated_at")),
        }

    if not metadata:
        metadata = _fallback_incomplete_metadata(model, root)

    newest = _float_metadata(metadata.get("newest_incomplete_mtime"))
    oldest = _float_metadata(metadata.get("oldest_incomplete_mtime"))
    incomplete_count = _int_metadata(metadata.get("incomplete_count"))
    incomplete_bytes = _int_metadata(metadata.get("incomplete_bytes"))
    generated_at = _float_metadata(metadata.get("status_generated_at"))
    if newest <= 0:
        return {
            "status": "unknown",
            "label": "fetch state unknown",
            "stalled_after_sec": stalled_after_sec,
            "incomplete_count": incomplete_count,
            "incomplete_bytes": incomplete_bytes,
            "status_generated_at": generated_at,
        }

    age = max(0.0, now - newest)
    active = age <= stalled_after_sec
    return {
        "status": "active" if active else "stalled",
        "label": "actively downloading" if active else "stalled or stopped",
        "stalled_after_sec": stalled_after_sec,
        "last_progress_at": newest,
        "last_progress_age_sec": age,
        "oldest_incomplete_mtime": oldest,
        "incomplete_count": incomplete_count,
        "incomplete_bytes": incomplete_bytes,
        "status_generated_at": generated_at,
    }


def hf_model_cache_details(
    model_id: str,
    cache_dir: str | None = None,
    *,
    stalled_after_sec: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    model = (model_id or "").strip()
    root = _hf_cache_root(cache_dir)
    if not model or not root.exists():
        return {"state": None, "fetch_activity": None}

    state = hf_model_cache_state(model, str(root))
    metadata = _model_status_metadata(model, root)
    threshold = float(stalled_after_sec if stalled_after_sec is not None else getattr(S, "MLX_FETCH_STALLED_AFTER_SEC", 600) or 600)
    current_time = float(now if now is not None else time.time())
    fetch_activity = _fetch_activity_from_metadata(
        model,
        state,
        root,
        metadata,
        stalled_after_sec=threshold,
        now=current_time,
    )
    return {
        "state": state,
        "fetch_activity": fetch_activity,
        "metadata": metadata,
    }


def hf_model_cache_entries(cache_dir: str | None = None) -> dict[str, str]:
    """Return visible Hugging Face model cache states keyed by repo id."""

    root = _hf_cache_root(cache_dir)
    if not root.exists():
        return {}

    metadata_payload = _cache_status_payload(root)
    metadata_models = metadata_payload.get("models") if isinstance(metadata_payload.get("models"), dict) else {}
    entries: dict[str, str] = {}
    metadata_items = metadata_models.items() if isinstance(metadata_models, dict) else []
    for model_id, metadata in metadata_items:
        if not isinstance(model_id, str) or not isinstance(metadata, dict):
            continue
        state = str(metadata.get("state") or "").strip()
        if state:
            entries[model_id] = state

    candidates: list[Path] = []
    for base in (root / "hub", root):
        if not base.exists():
            continue
        try:
            candidates.extend(path for path in base.iterdir() if path.is_dir() and path.name.startswith("models--"))
        except Exception:
            continue

    for path in candidates:
        repo_id = path.name.removeprefix("models--").replace("--", "/").strip()
        if not repo_id:
            continue
        if repo_id in entries:
            continue
        state = hf_model_cache_state(repo_id, str(root))
        if state:
            entries[repo_id] = state
    return entries


def purge_hf_model_cache(model_id: str, cache_dir: str | None = None) -> dict[str, Any]:
    model = (model_id or "").strip()
    mirror_root = _hf_cache_root(cache_dir)
    source_root = _hf_cache_source_root()
    purge_roots = []
    for root in (source_root, mirror_root):
        if root and root.exists():
            purge_roots.append(root)
    if not model or not purge_roots:
        return {
            "model": model,
            "cache_root": str(mirror_root),
            "source_root": str(source_root),
            "removed_paths": [],
            "metadata_updated": False,
        }

    resolved_roots = {root.resolve() for root in purge_roots}
    removed_paths: list[str] = []
    for root in purge_roots:
        for repo in _hf_repo_cache_dirs(model, str(root)):
            if not repo.exists():
                continue
            resolved_repo = repo.resolve()
            if not any(resolved_repo == resolved_root or resolved_root in resolved_repo.parents for resolved_root in resolved_roots):
                raise ValueError(f"Refusing to purge cache outside the configured roots: {repo}")
            shutil.rmtree(repo)
            removed_paths.append(str(repo))

    metadata_updated = False
    for root in purge_roots:
        metadata_path = root / ".nexus_cache_status.json"
        if not metadata_path.exists():
            continue
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        models = payload.get("models")
        if not isinstance(models, dict):
            models = {}
        if model in models:
            models.pop(model, None)
            payload["models"] = models
            payload["generated_at"] = time.time()
            metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            metadata_updated = True

    return {
        "model": model,
        "cache_root": str(mirror_root),
        "source_root": str(source_root),
        "removed_paths": removed_paths,
        "metadata_updated": metadata_updated,
    }


def model_unavailable_reason(backend: str, model: str) -> Optional[str]:
    if backend_provider_name(backend) != "mlx":
        return None
    state = hf_model_cache_state(model)
    if state in {"missing", "fetching"}:
        return state
    return None


def fallback_target_for_backend(backend: str) -> Optional[tuple[str, str]]:
    if backend_provider_name(backend) != "mlx":
        return None

    fallback_backend = (getattr(S, "MLX_FALLBACK_BACKEND", "") or backend or "").strip()
    if not fallback_backend:
        return None

    fallback_model = (getattr(S, "MLX_FALLBACK_MODEL", "") or "").strip()
    fallback_provider = backend_provider_name(fallback_backend)
    if not fallback_model:
        if fallback_provider == "vllm":
            fallback_model = (getattr(S, "VLLM_MODEL_FAST", "") or getattr(S, "VLLM_MODEL_DEFAULT", "") or "").strip()
        elif fallback_provider == "mlx":
            fallback_model = (getattr(S, "MLX_MODEL_FAST", "") or getattr(S, "MLX_MODEL_DEFAULT", "") or "").strip()

    if not fallback_model:
        return None
    return fallback_backend, fallback_model


def route_with_model_fallback(route):
    reason = model_unavailable_reason(route.backend, route.model)
    if not reason:
        return route

    fallback = fallback_target_for_backend(route.backend)
    if not fallback:
        return route
    fallback_backend, fallback_model = fallback

    if fallback_backend == route.backend and fallback_model == route.model:
        return route

    fallback_reason = model_unavailable_reason(fallback_backend, fallback_model)
    if fallback_reason:
        return route

    return replace(
        route,
        backend=fallback_backend,
        model=fallback_model,
        reason=f"{route.reason}->fallback:{reason}:{route.backend}:{route.model}",
    )
