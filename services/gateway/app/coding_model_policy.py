from __future__ import annotations

from typing import Any, Dict, List

from app.backends import backend_provider_name
from app import mlx_huge_lane
from app.model_aliases import ModelAlias, get_aliases


TRACK_CODER_MODEL = "coder"
_TRACKING_VALUES = {"", "auto", TRACK_CODER_MODEL}
_NON_CODING_ALIAS_VALUES = {"coder", "mlx", "long", "embeddings", "embeddings-fallback"}


def _model_label(model: str) -> str:
    info = mlx_huge_lane.model_info(model)
    return str(info.get("label") or model.rsplit("/", 1)[-1] or model)


def _strip_mlx_prefix(model: str) -> str:
    value = str(model or "").strip()
    lower = value.lower()
    for prefix in ("mlx:", "local_mlx:", "local-mlx:"):
        if lower.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


def _alias_label(alias_name: str, alias: ModelAlias) -> str:
    name = str(alias_name or "").strip()
    model = str(alias.upstream_model or "").strip()
    tail = model.rsplit("/", 1)[-1] if model else ""
    backend = backend_provider_name(alias.backend) or str(alias.backend or "").strip()
    if backend and tail:
        return f"{name} ({backend}: {tail})"
    if tail:
        return f"{name} ({tail})"
    return name


def _coding_aliases() -> Dict[str, ModelAlias]:
    out: Dict[str, ModelAlias] = {}
    for alias_name, alias in get_aliases().items():
        key = str(alias_name or "").strip().lower()
        if not key or key in _NON_CODING_ALIAS_VALUES:
            continue
        if alias.tools is False:
            continue
        model = _strip_mlx_prefix(alias.upstream_model)
        if backend_provider_name(alias.backend) == "mlx" and mlx_huge_lane.is_huge_model(model):
            continue
        out[key] = alias
    return out


def current_coder_model() -> str:
    if not mlx_huge_lane.enabled():
        return ""
    state = mlx_huge_lane.load_state()
    return (
        str(state.get("active_model") or "").strip()
        or str(state.get("route_model") or "").strip()
        or mlx_huge_lane.default_model()
    )


def is_tracking_coder_model(model: str) -> bool:
    return str(model or "").strip().lower() in _TRACKING_VALUES


def pinned_huge_model(model: str) -> str:
    candidate = _strip_mlx_prefix(model)
    if not candidate or is_tracking_coder_model(candidate):
        return ""
    return candidate if mlx_huge_lane.is_huge_model(candidate) else ""


def describe_workspace_model(model: str) -> Dict[str, Any]:
    raw = str(model or "").strip()
    state = mlx_huge_lane.load_state()
    active_model = str(state.get("active_model") or "").strip()
    route_model = str(state.get("route_model") or "").strip()
    current_model = current_coder_model()
    selected_model = raw or TRACK_CODER_MODEL
    huge_model = pinned_huge_model(selected_model)

    if is_tracking_coder_model(selected_model):
        return {
            "selected_model": TRACK_CODER_MODEL,
            "display_model": TRACK_CODER_MODEL,
            "resolved_model": current_model,
            "tracks_coder": True,
            "huge_model": current_model,
            "active_huge_model": active_model,
            "route_huge_model": route_model,
            "status": "tracking",
            "status_label": "Tracking current coder",
            "run_policy": "immediate",
            "warning": "",
            "recommended_model": "",
        }

    if huge_model:
        if huge_model == active_model:
            return {
                "selected_model": selected_model,
                "display_model": huge_model,
                "resolved_model": huge_model,
                "tracks_coder": False,
                "huge_model": huge_model,
                "active_huge_model": active_model,
                "route_huge_model": route_model,
                "status": "ready",
                "status_label": "Loaded",
                "run_policy": "immediate",
                "warning": "",
                "recommended_model": "",
            }
        active_label = _model_label(active_model) if active_model else "none"
        pinned_label = _model_label(huge_model)
        return {
            "selected_model": selected_model,
            "display_model": huge_model,
            "resolved_model": huge_model,
            "tracks_coder": False,
            "huge_model": huge_model,
            "active_huge_model": active_model,
            "route_huge_model": route_model,
            "status": "idle_only",
            "status_label": "Idle only",
            "run_policy": "idle_only",
            "warning": (
                f"This workspace is pinned to {pinned_label}, but the loaded coder model is {active_label}. "
                "It will only run during idle periods after that huge model is loaded. "
                "Switch this workspace to coder to track the current loaded model."
            ),
            "recommended_model": TRACK_CODER_MODEL,
        }

    alias = _coding_aliases().get(selected_model.lower())
    if alias is not None:
        return {
            "selected_model": selected_model,
            "display_model": selected_model,
            "resolved_model": str(alias.upstream_model or "").strip(),
            "tracks_coder": False,
            "huge_model": "",
            "active_huge_model": active_model,
            "route_huge_model": route_model,
            "status": "alias",
            "status_label": "Alias",
            "run_policy": "immediate",
            "warning": "",
            "recommended_model": "",
            "backend": alias.backend,
        }

    return {
        "selected_model": selected_model,
        "display_model": selected_model,
        "resolved_model": selected_model,
        "tracks_coder": False,
        "huge_model": "",
        "active_huge_model": active_model,
        "route_huge_model": route_model,
        "status": "custom",
        "status_label": "Custom",
        "run_policy": "immediate",
        "warning": "",
        "recommended_model": "",
    }


def options_payload() -> Dict[str, Any]:
    state = mlx_huge_lane.load_state()
    current_model = current_coder_model()
    current_label = _model_label(current_model) if current_model else "current huge model"
    active_model = str(state.get("active_model") or "").strip()
    candidates = state.get("candidates") if isinstance(state.get("candidates"), list) else []
    options: List[Dict[str, Any]] = [
        {
            "value": TRACK_CODER_MODEL,
            "label": f"Track current coder ({current_label})",
            "kind": "tracking",
            "model": current_model,
            "status": "tracking",
            "run_policy": "immediate",
        }
    ]
    for candidate in candidates:
        model = str((candidate or {}).get("model") or "").strip()
        if not model:
            continue
        label = str((candidate or {}).get("label") or _model_label(model))
        is_active = model == active_model
        options.append(
            {
                "value": model,
                "label": f"{label} ({'loaded' if is_active else 'idle only'})",
                "kind": "huge",
                "model": model,
                "status": "ready" if is_active else "idle_only",
                "run_policy": "immediate" if is_active else "idle_only",
                "estimated_load_sec": int((candidate or {}).get("estimated_load_sec") or 0),
                "estimated_memory_gb": (candidate or {}).get("estimated_memory_gb"),
            }
        )
    for alias_name, alias in sorted(_coding_aliases().items()):
        model = str(alias.upstream_model or "").strip()
        options.append(
            {
                "value": alias_name,
                "label": _alias_label(alias_name, alias),
                "kind": "alias",
                "model": model,
                "backend": alias.backend,
                "status": "alias",
                "run_policy": "immediate",
                "tools": alias.tools,
                "context_window": alias.context_window,
                "max_tokens_cap": alias.max_tokens_cap,
            }
        )
    return {
        "track_model": TRACK_CODER_MODEL,
        "current_coder_model": current_model,
        "active_huge_model": active_model,
        "route_huge_model": str(state.get("route_model") or "").strip(),
        "huge_lane": state,
        "options": options,
    }
