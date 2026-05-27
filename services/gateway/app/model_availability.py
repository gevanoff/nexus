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


def hf_model_cache_entries(cache_dir: str | None = None) -> dict[str, str]:
    """Return visible Hugging Face model cache states keyed by repo id."""

    root = Path((cache_dir or getattr(S, "MLX_HF_CACHE_DIR", "") or "").strip())
    if not root.exists():
        return {}

    candidates: list[Path] = []
    for base in (root / "hub", root):
        if not base.exists():
            continue
        try:
            candidates.extend(path for path in base.iterdir() if path.is_dir() and path.name.startswith("models--"))
        except Exception:
            continue

    entries: dict[str, str] = {}
    for path in candidates:
        repo_id = path.name.removeprefix("models--").replace("--", "/").strip()
        if not repo_id:
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
