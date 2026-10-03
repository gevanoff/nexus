from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal

import httpx
from fastapi import HTTPException

from app.backends import get_registry
from app.config import S
from app.health_checker import check_backend_ready, get_health_checker
from app.resources_snapshot import call_lifecycle_manager, lifecycle_manager_base_url, lifecycle_timeout


@dataclass
class TtsResult:
    kind: Literal["audio", "json"]
    content_type: str
    audio: bytes | None = None
    payload: Dict[str, Any] | None = None
    gateway: Dict[str, Any] = field(default_factory=dict)


def _effective_tts_base_url(*, backend_class: str) -> str:
    try:
        reg = get_registry()
        cfg = reg.get_backend(backend_class)
        if cfg and isinstance(cfg.base_url, str) and cfg.base_url.strip():
            return cfg.base_url.strip().rstrip("/")
    except Exception:
        pass
    return (getattr(S, "TTS_BASE_URL", "") or "").strip().rstrip("/")


def _effective_timeout_sec() -> float:
    try:
        return float(getattr(S, "TTS_TIMEOUT_SEC", 300.0) or 300.0)
    except Exception:
        return 300.0


def _activation_wait_timeout_sec(plan: Dict[str, Any] | None) -> float:
    fallback = max(float(lifecycle_timeout()), 120.0)
    if not isinstance(plan, dict):
        return fallback
    backend = plan.get("backend")
    if not isinstance(backend, dict):
        return fallback
    try:
        configured = float(backend.get("health_timeout_sec") or fallback)
    except Exception:
        return fallback
    return max(1.0, configured)


async def ensure_tts_backend_ready(
    backend_class: str,
    *,
    reason: str,
    route_kind: str = "tts",
) -> Dict[str, Any] | None:
    """Policy-aware activation plus authoritative readiness refresh for TTS."""
    backend_class = str(backend_class or "").strip()
    if not backend_class:
        raise HTTPException(status_code=400, detail="backend_class required")

    plan: Dict[str, Any] | None = None
    if lifecycle_manager_base_url():
        try:
            plan = await call_lifecycle_manager(
                "POST",
                "/v1/lifecycle/ensure",
                json_body={
                    "backend_class": backend_class,
                    "route_kind": route_kind,
                    "reason": str(reason or "tts").strip() or "tts",
                    "confirmed": False,
                    "allow_disruptive": False,
                },
                timeout=max(lifecycle_timeout(), 120.0),
            )
        except HTTPException as exc:
            # Older/unavailable lifecycle deployments may not support ensure.
            # Readiness below remains the fail-closed authority.
            if exc.status_code not in {404, 503}:
                raise
        if isinstance(plan, dict):
            decision = str(plan.get("decision") or "").strip().lower()
            if decision in {"requires_confirmation", "blocked", "observe_only"}:
                raise HTTPException(
                    status_code=409 if decision == "requires_confirmation" else 503,
                    detail={
                        "error": "tts_backend_activation_blocked",
                        "backend_class": backend_class,
                        "decision": decision,
                        "lifecycle_plan": plan,
                    },
                )

    checker = get_health_checker()
    started_backends = {
        str(item or "").strip()
        for item in (plan.get("start") if isinstance(plan, dict) and isinstance(plan.get("start"), list) else [])
        if str(item or "").strip()
    }
    wait_for_startup = backend_class in started_backends
    deadline = time.monotonic() + _activation_wait_timeout_sec(plan) if wait_for_startup else 0.0

    while True:
        status = await checker.refresh_backend(backend_class)
        if status is None or status.raw_ready is not False:
            break
        if not wait_for_startup or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(1.0, max(0.05, deadline - time.monotonic())))

    if status is not None and status.raw_ready is False:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "backend_not_ready",
                "backend_class": backend_class,
                "message": status.raw_error or status.error or "readiness check failed",
            },
        )
    check_backend_ready(backend_class, route_kind=route_kind)
    return plan


def _effective_generate_path() -> str:
    p = (getattr(S, "TTS_GENERATE_PATH", "") or "/v1/audio/speech").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _speed_range_for_backend(backend_class: str) -> tuple[float, float]:
    key = str(backend_class or "").strip().lower()
    # Keep the current operational range by default; backends can diverge later.
    if "lux" in key:
        return 0.5, 2.0
    if "qwen" in key:
        return 0.5, 2.0
    if "pocket" in key:
        return 0.5, 2.0
    return 0.5, 2.0


def _neutral_speed_for_backend(backend_class: str) -> float:
    key = str(backend_class or "").strip().lower()
    if "lux" in key:
        return 0.85
    return 1.0


def _normalize_speed(speed_value: Any, *, backend_class: str) -> float | None:
    try:
        speed = float(speed_value)
    except Exception:
        return None

    min_speed, max_speed = _speed_range_for_backend(backend_class)

    # Backward compatibility: existing callers already send backend-domain speed.
    if speed <= 2.0:
        return _clamp(speed, min_speed, max_speed)

    # Normalized UI scale: 1..10 with 5 mapping to a backend-specific neutral speed.
    if 1.0 <= speed <= 10.0:
        neutral_speed = _clamp(_neutral_speed_for_backend(backend_class), min_speed, max_speed)
        if speed <= 5.0:
            normalized = (speed - 1.0) / 4.0
            mapped = min_speed + (normalized * (neutral_speed - min_speed))
            return _clamp(mapped, min_speed, max_speed)
        normalized = (speed - 5.0) / 5.0
        mapped = neutral_speed + (normalized * (max_speed - neutral_speed))
        return _clamp(mapped, min_speed, max_speed)

    # Out-of-range values are clamped to backend limits.
    return _clamp(speed, min_speed, max_speed)


def _normalize_payload(body: Dict[str, Any], *, backend_class: str) -> Dict[str, Any]:
    payload = dict(body)
    if "text" not in payload and isinstance(payload.get("input"), str):
        payload["text"] = payload.get("input")

    if "input" not in payload and isinstance(payload.get("text"), str):
        payload["input"] = payload.get("text")

    if "voice" in payload and payload["voice"] is not None:
        payload["voice"] = str(payload["voice"]).strip()
        if not payload["voice"]:
            payload.pop("voice", None)

    if "speed" in payload:
        normalized = _normalize_speed(payload.get("speed"), backend_class=backend_class)
        if normalized is None:
            payload.pop("speed", None)
        else:
            payload["speed"] = normalized

    return payload


def _decode_audio_from_json(payload: Dict[str, Any]) -> tuple[bytes, str] | None:
    key = None
    for cand in ("audio_base64", "audio", "audio_data"):
        if isinstance(payload.get(cand), str) and payload.get(cand):
            key = cand
            break
    if not key:
        return None

    raw_value = str(payload[key])
    content_type = (
        payload.get("content_type")
        or payload.get("mime_type")
        or payload.get("format")
        or "audio/wav"
    )

    if raw_value.startswith("data:") and "," in raw_value:
        header, raw_value = raw_value.split(",", 1)
        mime = header.split(";")[0].replace("data:", "")
        if mime:
            content_type = mime

    try:
        raw = base64.b64decode(raw_value.encode("ascii"), validate=False)
    except Exception:
        return None
    return raw, str(content_type).strip() or "audio/wav"


async def generate_tts(*, backend_class: str, body: Dict[str, Any]) -> TtsResult:
    base = _effective_tts_base_url(backend_class=backend_class)
    if not base:
        raise RuntimeError(
            "TTS_BASE_URL is required (or set base_url for the TTS backend in backends_config.yaml)"
        )

    timeout = _effective_timeout_sec()
    path = _effective_generate_path()
    payload = _normalize_payload(body, backend_class=backend_class)

    started = time.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base}{path}", json=payload)

    if r.status_code < 200 or r.status_code >= 300:
        detail: Any
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"{backend_class} HTTP {r.status_code}: {detail}")

    gateway = {
        "backend": backend_class,
        "backend_class": backend_class,
        "upstream_base_url": base,
        "upstream_path": path,
        "upstream_latency_ms": round((time.time() - started) * 1000.0, 1),
        "voice": payload.get("voice") if isinstance(payload.get("voice"), str) else None,
        "speed": payload.get("speed") if isinstance(payload.get("speed"), (int, float)) else None,
    }

    content_type = r.headers.get("content-type", "application/octet-stream")
    if "application/json" in (content_type or ""):
        try:
            payload_json = r.json()
        except Exception:
            payload_json = None

        if isinstance(payload_json, dict):
            decoded = _decode_audio_from_json(payload_json)
            if decoded:
                raw, decoded_type = decoded
                return TtsResult(
                    kind="audio",
                    content_type=decoded_type,
                    audio=raw,
                    payload=payload_json,
                    gateway=gateway,
                )
            return TtsResult(
                kind="json",
                content_type=content_type,
                payload=payload_json,
                gateway=gateway,
            )

    return TtsResult(kind="audio", content_type=content_type, audio=r.content, gateway=gateway)
