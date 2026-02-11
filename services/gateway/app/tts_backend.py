from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal

import httpx

from app.backends import get_registry
from app.config import S


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
        return float(getattr(S, "TTS_TIMEOUT_SEC", 60.0) or 60.0)
    except Exception:
        return 60.0


def _effective_generate_path() -> str:
    p = (getattr(S, "TTS_GENERATE_PATH", "") or "/v1/audio/speech").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _normalize_payload(body: Dict[str, Any]) -> Dict[str, Any]:
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
        try:
            speed = float(payload["speed"])
            payload["speed"] = max(0.5, min(2.0, speed))
        except Exception:
            payload.pop("speed", None)

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
    payload = _normalize_payload(body)

    started = time.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base}{path}", json=payload)

    if r.status_code < 200 or r.status_code >= 300:
        detail: Any
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"pocket-tts HTTP {r.status_code}: {detail}")

    gateway = {
        "backend": "pocket-tts",
        "backend_class": backend_class,
        "upstream_base_url": base,
        "upstream_path": path,
        "upstream_latency_ms": round((time.time() - started) * 1000.0, 1),
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
