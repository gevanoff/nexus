from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from app.config import S


DEFAULT_HUGE_MODELS = [
    "mlx-community/GLM-5.2-DQ4plus-q8",
    "mlx-community/MiniMax-M2.5-8bit",
    "mlx-community/DeepSeek-R1-0528-4bit",
]

MODEL_ESTIMATES: Dict[str, Dict[str, Any]] = {
    "mlx-community/GLM-5.2-DQ4plus-q8": {
        "label": "GLM-5.2 DQ4plus-q8",
        "estimated_load_sec": 420,
        "estimated_memory_gb": 465,
    },
    "mlx-community/MiniMax-M2.5-8bit": {
        "label": "MiniMax M2.5 8-bit",
        "estimated_load_sec": 90,
        "estimated_memory_gb": 240,
    },
    "mlx-community/DeepSeek-R1-0528-4bit": {
        "label": "DeepSeek R1 0528 4-bit",
        "estimated_load_sec": 110,
        "estimated_memory_gb": 370,
    },
}


def enabled() -> bool:
    return bool(getattr(S, "MLX_HUGE_LANE_ENABLED", True))


def state_path() -> Path:
    configured = str(
        getattr(S, "MLX_HUGE_LANE_STATE_PATH", "")
        or "/var/lib/gateway/data/mlx_huge_lane.json"
    ).strip()
    return Path(configured)


def configured_models() -> List[str]:
    raw = str(getattr(S, "MLX_HUGE_MODELS", "") or "").strip()
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else []
    models = values or DEFAULT_HUGE_MODELS
    out: List[str] = []
    seen = set()
    for model in models:
        if model and model not in seen:
            seen.add(model)
            out.append(model)
    return out


def is_huge_model(model: str) -> bool:
    return str(model or "").strip() in set(configured_models())


def default_model() -> str:
    configured_default = str(
        getattr(S, "MLX_HUGE_LANE_DEFAULT_MODEL", "")
        or getattr(S, "MLX_MODEL_STRONG", "")
        or ""
    ).strip()
    models = configured_models()
    if configured_default in models:
        return configured_default
    return models[0] if models else configured_default


def model_info(model: str) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    estimate = dict(MODEL_ESTIMATES.get(model_id) or {})
    estimate.setdefault("label", model_id.rsplit("/", 1)[-1] if model_id else "")
    estimate.setdefault("estimated_load_sec", 120)
    estimate.setdefault("estimated_memory_gb", None)
    estimate["model"] = model_id
    return estimate


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

    route_model = target_model if status == "switching" and target_model else active_model
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
                **model_info(model),
                "active": model == active_model,
                "target": model == target_model and status == "switching",
            }
            for model in models
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
