from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from app.backends import backend_provider_name
from app.config import S


def _hf_repo_cache_name(model_id: str) -> str:
    safe_id = (model_id or "").strip().replace("/", "--")
    return f"models--{safe_id}"


def _hf_repo_cache_dirs(model_id: str, cache_dir: str) -> list[Path]:
    name = _hf_repo_cache_name(model_id)
    root = Path(cache_dir)
    return [root / "hub" / name, root / name]


def hf_model_cache_state(model_id: str, cache_dir: str | None = None) -> Optional[str]:
    """Return cached/missing/fetching/unknown for a Hugging Face model cache.

    ``None`` means the gateway cannot inspect that cache, usually because the
    host-native cache is not mounted into the container.
    """

    model = (model_id or "").strip()
    root = Path((cache_dir or getattr(S, "MLX_HF_CACHE_DIR", "") or "").strip())
    if not model or not root.exists():
        return None

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


def model_unavailable_reason(backend: str, model: str) -> Optional[str]:
    if backend_provider_name(backend) != "mlx":
        return None
    state = hf_model_cache_state(model)
    if state in {"missing", "fetching"}:
        return state
    return None


def route_with_model_fallback(route):
    reason = model_unavailable_reason(route.backend, route.model)
    if not reason:
        return route

    fallback = (getattr(S, "MLX_FALLBACK_MODEL", "") or "").strip()
    if not fallback or fallback == route.model:
        return route

    fallback_reason = model_unavailable_reason(route.backend, fallback)
    if fallback_reason:
        return route

    return replace(route, model=fallback, reason=f"{route.reason}->fallback:{reason}:{route.model}")
