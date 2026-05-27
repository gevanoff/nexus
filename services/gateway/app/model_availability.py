from __future__ import annotations

import json
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
        return metadata_state

    repos = [repo for repo in _hf_repo_cache_dirs(model, str(root)) if repo.exists()]
    if not repos:
        return "missing"

    for repo in repos:
        if any(repo.glob("blobs/*.incomplete")):
            return "fetching"

    for repo in repos:
        snapshots = repo / "snapshots"
        try:
            if any(True for _path in snapshots.glob("*/*")):
                return "cached"
        except Exception:
            return None
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
    if state != "fetching":
        return None

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
