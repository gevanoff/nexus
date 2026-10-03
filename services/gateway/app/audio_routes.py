from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
import httpx
from urllib.parse import quote

from app.music_backend import _effective_heartmula_base_url, _effective_timeout_sec
from app.config import S
from app.auth import require_bearer
from app.audio_cache import resolve_audio_cache_path

router = APIRouter()


@router.get("/ui/heartmula/audio/{filename}")
async def proxy_heartmula_audio(filename: str):
    """Proxy a HeartMula audio file through the gateway.

    This ensures the browser fetches the audio from the gateway (same origin) rather
    than directly from HeartMula, allowing firewalling and access control.
    """
    base = _effective_heartmula_base_url(backend_class=getattr(S, "MUSIC_BACKEND_CLASS", "heartmula_music"))
    if not base:
        raise HTTPException(status_code=404, detail="HeartMula base URL not configured")

    # Construct only the allowed upstream URL under HEARTMULA_BASE_URL
    upstream = f"{base.rstrip('/')}/audio/{quote(filename)}"

    timeout = _effective_timeout_sec()
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.get(upstream, timeout=timeout)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"failed to fetch upstream audio: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"upstream audio status: {r.status_code}")

    content_type = r.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(r.aiter_bytes(), media_type=content_type)


@router.get("/v1/audio/artifacts/{filename}")
async def api_audio_artifact(req: Request, filename: str):
    require_bearer(req)
    path = resolve_audio_cache_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="audio artifact not found")
    return FileResponse(path)
