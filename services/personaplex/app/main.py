import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


app = FastAPI(title="PersonaPlex Shim", version="0.1")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _now() -> int:
    return int(time.time())


def _upstream_base_url() -> Optional[str]:
    url = _env("PERSONAPLEX_UPSTREAM_BASE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _timeout_sec() -> int:
    return _int_env("PERSONAPLEX_TIMEOUT_SEC", 120)


def _workdir() -> str:
    return _env("PERSONAPLEX_WORKDIR", "/data/app") or "/data/app"


def _model_id() -> str:
    return _env("PERSONAPLEX_MODEL", "personaplex") or "personaplex"


def _log_path(name: str) -> str:
    log_dir = _env("PERSONAPLEX_LOG_DIR", "/data/logs") or "/data/logs"
    return os.path.join(log_dir, name)


def _append_log(path: str, data: bytes, job_id: str) -> None:
    if not data:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} job={job_id} ---\n".encode())
            handle.write(data)
            if not data.endswith(b"\n"):
                handle.write(b"\n")
    except OSError:
        return


@app.get("/healthz")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "personaplex-shim"}


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": _model_id(), "object": "model", "owned_by": "nvidia"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(payload: Dict[str, Any]) -> Any:
    upstream = _upstream_base_url()
    if upstream:
        timeout = httpx.Timeout(connect=10.0, read=float(_timeout_sec()), write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{upstream}/v1/chat/completions", json=payload)
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return resp.json()
    raise HTTPException(
        status_code=501,
        detail={
            "error": "personaplex_rest_unavailable",
            "detail": "This Nexus service no longer accepts an external run command. Configure PERSONAPLEX_UPSTREAM_BASE_URL or use the live PersonaPlex UI.",
        },
    )


@app.get("/readyz")
def readyz() -> JSONResponse:
    if _upstream_base_url():
        return JSONResponse(status_code=200, content={"ok": True})
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": "missing_configuration",
            "detail": "Set PERSONAPLEX_UPSTREAM_BASE_URL for REST proxying or use the live PersonaPlex UI.",
        },
    )