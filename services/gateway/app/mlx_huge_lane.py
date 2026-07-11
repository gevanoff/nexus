from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from app.config import S
from app.model_aliases import get_aliases


def enabled() -> bool:
    return bool(getattr(S, "MLX_HUGE_LANE_ENABLED", True))


def state_path() -> Path:
    configured = str(
        getattr(S, "MLX_HUGE_LANE_STATE_PATH", "")
        or "/var/lib/gateway/data/mlx_huge_lane.json"
    ).strip()
    return Path(configured)


def configured_candidates() -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen_models = set()
    for alias_name, alias in get_aliases().items():
        model = str(alias.upstream_model or "").strip()
        if not alias.huge_candidate or not model or model in seen_models:
            continue
        seen_models.add(model)
        candidates.append(
            {
                "alias": str(alias_name or "").strip().lower(),
                "model": model,
                "label": alias.label or model.rsplit("/", 1)[-1],
                "estimated_load_sec": alias.estimated_load_sec or 120,
                "estimated_memory_gb": alias.estimated_memory_gb,
                "default": bool(alias.huge_default),
                "switchable": bool(alias.huge_switchable),
            }
        )
    return candidates


def configured_models() -> List[str]:
    return [str(candidate["model"]) for candidate in configured_candidates()]


def is_huge_model(model: str) -> bool:
    value = str(model or "").strip()
    return any(value in {candidate["alias"], candidate["model"]} for candidate in configured_candidates())


def resolve_model(model_or_alias: str) -> str:
    value = str(model_or_alias or "").strip()
    for candidate in configured_candidates():
        if value in {candidate["alias"], candidate["model"]}:
            return str(candidate["model"])
    return ""


def resolve_request_model(model_or_alias: str) -> str:
    """Resolve any request alias that ultimately targets a Huge MLX model."""
    value = str(model_or_alias or "").strip()
    if value.lower() in {"coder", "mlx", "long"}:
        return route_model() or default_model()
    candidate = resolve_model(value)
    if candidate:
        return candidate
    alias = get_aliases().get(value.lower())
    upstream = str(alias.upstream_model or "").strip() if alias is not None else ""
    return upstream if is_huge_model(upstream) else ""


def default_model() -> str:
    candidates = configured_candidates()
    for candidate in candidates:
        if candidate["default"]:
            return str(candidate["model"])
    strong_model = str(getattr(S, "MLX_MODEL_STRONG", "") or "").strip()
    if strong_model and any(candidate["model"] == strong_model for candidate in candidates):
        return strong_model
    return str(candidates[0]["model"]) if candidates else strong_model


def model_info(model: str) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    for candidate in configured_candidates():
        if model_id in {candidate["alias"], candidate["model"]}:
            return dict(candidate)
    return {
        "alias": "",
        "model": model_id,
        "label": model_id.rsplit("/", 1)[-1] if model_id else "",
        "estimated_load_sec": 120,
        "estimated_memory_gb": None,
        "default": False,
    }


def _read_raw_state() -> Dict[str, Any]:
    path = state_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def load_state() -> Dict[str, Any]:
    models = configured_models()
    payload = _read_raw_state()
    active_model = str(payload.get("active_model") or "").strip()
    if active_model not in models:
        active_model = default_model()

    status = str(payload.get("status") or "ready").strip().lower()
    if status not in {"ready", "switching", "error"}:
        status = "ready"

    target_model = str(payload.get("target_model") or "").strip()
    if target_model not in models:
        target_model = ""

    # Requests stay pinned to the last confirmed resident model. A transition
    # is an administrative operation, never an implicit request-time route.
    route_model = active_model
    now = time.time()
    started_at = float(payload.get("switch_started_at") or 0)
    elapsed_sec = max(0.0, now - started_at) if status == "switching" and started_at else 0.0
    estimate_sec = int(model_info(route_model).get("estimated_load_sec") or 120)

    state = {
        "enabled": enabled(),
        "active_model": active_model,
        "target_model": target_model,
        "route_model": route_model,
        "status": status,
        "status_label": "Switching" if status == "switching" else ("Error" if status == "error" else "Ready"),
        "message": str(payload.get("message") or "").strip(),
        "error": str(payload.get("error") or "").strip(),
        "updated_at": float(payload.get("updated_at") or 0),
        "switch_started_at": started_at,
        "switch_completed_at": float(payload.get("switch_completed_at") or 0),
        "elapsed_sec": elapsed_sec,
        "estimated_load_sec": estimate_sec,
        "previous_model": str(payload.get("previous_model") or "").strip(),
        "candidates": [
            {
                **candidate,
                "active": candidate["model"] == active_model,
                "target": candidate["model"] == target_model and status == "switching",
            }
            for candidate in configured_candidates()
        ],
    }
    return state


def route_model() -> str:
    if not enabled():
        return ""
    return str(load_state().get("route_model") or "").strip()


def active_model() -> str:
    if not enabled():
        return ""
    return str(load_state().get("active_model") or "").strip()


def request_block(model: str) -> Dict[str, Any] | None:
    """Describe why a Huge request must not be sent to MLX right now."""
    model_id = str(model or "").strip()
    if not enabled() or not is_huge_model(model_id):
        return None

    state = load_state()
    status = str(state.get("status") or "ready")
    active = str(state.get("active_model") or "").strip()
    target = str(state.get("target_model") or "").strip()
    if status == "switching":
        return {
            "error": "mlx_huge_transition_in_progress",
            "message": "The resident MLX Huge model is being replaced by an administrator.",
            "active_model": active,
            "target_model": target,
            "retryable": True,
        }
    if status == "error":
        return {
            "error": "mlx_huge_transition_failed",
            "message": str(state.get("error") or "The resident MLX Huge model transition failed."),
            "active_model": active,
            "target_model": target,
            "retryable": False,
        }
    if not active or model_id != active:
        return {
            "error": "mlx_huge_model_not_resident",
            "message": "This Huge model must be selected manually in Model Admin before use.",
            "active_model": active,
            "requested_model": model_id,
            "retryable": False,
        }
    return None


def write_state(update: Dict[str, Any]) -> Dict[str, Any]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_raw_state()
    current.update(update)
    current["updated_at"] = time.time()
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)
    return load_state()


def mark_switching(model: str) -> Dict[str, Any]:
    state = load_state()
    return write_state(
        {
            "status": "switching",
            "previous_model": state.get("active_model") or "",
            "target_model": model,
            "message": f"Loading {model}",
            "error": "",
            "switch_started_at": time.time(),
            "switch_completed_at": 0,
        }
    )


def mark_ready(model: str, *, message: str = "") -> Dict[str, Any]:
    return write_state(
        {
            "status": "ready",
            "active_model": model,
            "target_model": "",
            "message": message or f"{model} is ready",
            "error": "",
            "switch_completed_at": time.time(),
        }
    )


def mark_error(model: str, error: str) -> Dict[str, Any]:
    return write_state(
        {
            "status": "error",
            "target_model": model,
            "message": f"Failed to load {model}",
            "error": error,
            "switch_completed_at": time.time(),
        }
    )
