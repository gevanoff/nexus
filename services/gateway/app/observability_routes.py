from __future__ import annotations

from typing import Any, Dict

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import S
from app.metrics import render_prometheus_text


router = APIRouter()


@router.get("/health")
@router.get("/health/", include_in_schema=False)
async def health():
    return {"ok": True}


@router.get("/readyz")
@router.get("/readyz/", include_in_schema=False)
async def readyz():
    from app.health_checker import get_health_checker

    checker = get_health_checker()
    status = checker.get_all_status()
    if not status:
        return {"ok": True, "ready": True, "detail": "no_backends"}

    ready = all(s.is_healthy and s.is_ready for s in status.values())
    payload = {
        "ok": bool(ready),
        "ready": bool(ready),
        "backends": {
            name: {
                "healthy": s.is_healthy,
                "ready": s.is_ready,
                "last_check": s.last_check,
                "error": s.error,
            }
            for name, s in status.items()
        },
    }
    if ready:
        return payload
    return JSONResponse(payload, status_code=503)


@router.head("/health", include_in_schema=False)
@router.head("/health/", include_in_schema=False)
async def health_head():
    return PlainTextResponse("", status_code=200)


@router.get("/health/upstreams")
async def health_upstreams():
    results: Dict[str, Any] = {"ok": True, "upstreams": {}}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            results["upstreams"]["ollama"] = {"ok": True, "status": r.status_code}
        except Exception as e:
            results["ok"] = False
            results["upstreams"]["ollama"] = {"ok": False, "error": str(e)}

        try:
            r = await client.get(f"{S.MLX_BASE_URL}/models")
            r.raise_for_status()
            results["upstreams"]["mlx"] = {"ok": True, "status": r.status_code}
        except Exception as e:
            results["ok"] = False
            results["upstreams"]["mlx"] = {"ok": False, "error": str(e)}

    return results


@router.get("/metrics")
async def metrics_endpoint():
    if not getattr(S, "METRICS_ENABLED", True):
        return PlainTextResponse("", status_code=404)
    return PlainTextResponse(render_prometheus_text(), media_type="text/plain; version=0.0.4")
