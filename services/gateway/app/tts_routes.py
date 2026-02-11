from __future__ import annotations

import io
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import require_bearer
from app.backends import check_capability, get_admission_controller
from app.config import S
from app.health_checker import check_backend_ready
from app.tts_backend import generate_tts


router = APIRouter()


def _coerce_body(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        alt = body.get("input")
        if not isinstance(alt, str) or not alt.strip():
            raise HTTPException(status_code=400, detail="text is required")
    return body


def _gateway_headers(meta: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return headers
    backend = meta.get("backend")
    backend_class = meta.get("backend_class")
    latency = meta.get("upstream_latency_ms")
    if backend:
        headers["x-gateway-backend"] = str(backend)
    if backend_class:
        headers["x-gateway-backend-class"] = str(backend_class)
    if latency is not None:
        headers["x-gateway-upstream-latency-ms"] = str(latency)
    return headers


async def _handle_tts(req: Request) -> StreamingResponse | JSONResponse:
    body = _coerce_body(await req.json())
    backend_class = (getattr(S, "TTS_BACKEND_CLASS", "") or "").strip() or "pocket_tts"

    check_backend_ready(backend_class, route_kind="tts")
    await check_capability(backend_class, "tts")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "tts")
    try:
        result = await generate_tts(backend_class=backend_class, body=body)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"tts backend error: {type(e).__name__}: {e}")
    finally:
        admission.release(backend_class, "tts")

    headers = _gateway_headers(result.gateway)
    if result.kind == "json":
        payload = result.payload
        if isinstance(payload, dict):
            payload.setdefault("_gateway", {}).update(result.gateway)
        return JSONResponse(payload or {}, headers=headers)

    if result.audio is None:
        raise HTTPException(status_code=502, detail="tts backend returned no audio")

    return StreamingResponse(iter([result.audio]), media_type=result.content_type, headers=headers)


@router.post("/v1/tts/generations")
async def tts_generations(req: Request):
    require_bearer(req)
    return await _handle_tts(req)


@router.post("/v1/audio/speech")
async def tts_audio_speech(req: Request):
    require_bearer(req)
    return await _handle_tts(req)
