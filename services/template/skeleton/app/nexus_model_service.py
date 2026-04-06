from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


ROUTES: dict[str, dict[str, Any]] = {
    "chat": {"path": "/v1/chat/completions", "capabilities": ["chat"], "hint": "messages"},
    "embeddings": {"path": "/v1/embeddings", "capabilities": ["embeddings"], "hint": "input"},
    "images": {"path": "/v1/images/generations", "capabilities": ["images"], "hint": "prompt"},
    "tts": {"path": "/v1/audio/speech", "capabilities": ["tts"], "hint": "input", "media_type": "audio/wav"},
    "ocr": {"path": "/v1/ocr", "capabilities": ["ocr"], "hint": "image or image_url"},
    "video": {"path": "/v1/videos/generations", "capabilities": ["video"], "hint": "prompt"},
    "music": {"path": "/v1/music/generations", "capabilities": ["music"], "hint": "prompt"},
    "json": {"path": "/v1/run", "capabilities": ["custom"], "hint": "arbitrary JSON payload"},
}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv_env(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _route_kind() -> str:
    route_kind = _env("NEXUS_ROUTE_KIND", "__ROUTE_KIND__").lower()
    if route_kind not in ROUTES:
        raise RuntimeError(f"Unsupported NEXUS_ROUTE_KIND: {route_kind}")
    return route_kind


def _route() -> dict[str, Any]:
    return ROUTES[_route_kind()]


def _service_name() -> str:
    return _env("NEXUS_SERVICE_NAME", "__SERVICE_NAME__")


def _service_title() -> str:
    return _env("NEXUS_SERVICE_TITLE", "__SERVICE_TITLE__")


def _service_description() -> str:
    return _env("NEXUS_SERVICE_DESCRIPTION", "__SERVICE_DESCRIPTION__")


def _resolve_mode() -> str:
    mode = _env("NEXUS_EXECUTION_MODE", "auto").lower()
    if mode in {"upstream", "command"}:
        return mode
    if _env("NEXUS_UPSTREAM_BASE_URL"):
        return "upstream"
    if _env("NEXUS_RUN_COMMAND"):
        return "command"
    return "unconfigured"


def _validate_body(body: dict[str, Any]) -> None:
    route_kind = _route_kind()
    if route_kind == "chat" and not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    if route_kind == "embeddings" and "input" not in body:
        raise HTTPException(status_code=400, detail="input is required")
    if route_kind in {"images", "video", "music"} and not str(body.get("prompt") or "").strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if route_kind == "tts" and not str(body.get("input") or body.get("text") or "").strip():
        raise HTTPException(status_code=400, detail="input is required")
    if route_kind == "ocr" and "image" not in body and "image_url" not in body:
        raise HTTPException(status_code=400, detail="image or image_url is required")


async def _runtime_error() -> dict[str, Any] | None:
    mode = _resolve_mode()
    if mode == "unconfigured":
        return {"reason": "missing_configuration", "detail": "Set NEXUS_UPSTREAM_BASE_URL or NEXUS_RUN_COMMAND."}
    if mode == "upstream":
        base = _env("NEXUS_UPSTREAM_BASE_URL")
        ready_paths = _csv_env("NEXUS_UPSTREAM_READY_PATHS", "/readyz,/healthz,/health,/v1/models")
        if _route()["path"] not in ready_paths:
            ready_paths.append(_route()["path"])
        timeout = httpx.Timeout(connect=5.0, read=float(_int_env("NEXUS_READYZ_TIMEOUT_SEC", 10)), write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for path in ready_paths:
                url = f"{base.rstrip('/')}{path if path.startswith('/') else '/' + path}"
                try:
                    response = await client.get(url)
                    if response.status_code < 400:
                        return None
                except Exception:
                    continue
        return {"reason": "upstream_unhealthy", "detail": f"Upstream readiness probes failed for {base}"}
    if not Path(_env("NEXUS_WORKDIR", "/app")).exists():
        return {"reason": "missing_workdir", "detail": f"Configured workdir does not exist: {_env('NEXUS_WORKDIR', '/app')}"}
    ready_command = _env("NEXUS_RUN_READY_COMMAND")
    if not ready_command:
        return None
    proc = await asyncio.create_subprocess_exec(
        _env("NEXUS_SHELL", "/bin/sh"),
        "-lc",
        ready_command,
        cwd=_env("NEXUS_WORKDIR", "/app"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=float(_int_env("NEXUS_READYZ_TIMEOUT_SEC", 10)))
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"reason": "ready_command_timeout", "detail": ready_command}
    if proc.returncode == 0:
        return None
    return {
        "reason": "ready_command_failed",
        "detail": {
            "returncode": proc.returncode,
            "stdout": (stdout_bytes or b"").decode(errors="ignore")[-2000:],
            "stderr": (stderr_bytes or b"").decode(errors="ignore")[-2000:],
        },
    }


async def _proxy_request(body: dict[str, Any]) -> StreamingResponse | JSONResponse | Any:
    base = _env("NEXUS_UPSTREAM_BASE_URL")
    if not base:
        raise HTTPException(status_code=503, detail="NEXUS_UPSTREAM_BASE_URL is not configured")
    endpoint = _env("NEXUS_UPSTREAM_ENDPOINT", _route()["path"])
    timeout = httpx.Timeout(connect=10.0, read=float(_int_env("NEXUS_TIMEOUT_SEC", 300)), write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base.rstrip('/')}{endpoint}", json=body)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise HTTPException(status_code=response.status_code, detail=detail)
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if _route_kind() == "tts" and not content_type.startswith("application/json"):
            media_type = content_type or _env("NEXUS_OUTPUT_MEDIA_TYPE", _route().get("media_type", "audio/wav"))
            return StreamingResponse(iter([response.content]), media_type=media_type)
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}


async def _run_command(body: dict[str, Any]) -> dict[str, Any]:
    command = _env("NEXUS_RUN_COMMAND")
    if not command:
        raise HTTPException(status_code=503, detail="NEXUS_RUN_COMMAND is not configured")
    with tempfile.TemporaryDirectory(prefix=f"{_service_name()}-") as tmpdir:
        workdir = Path(tmpdir)
        request_json = workdir / "request.json"
        output_json = workdir / "output.json"
        output_media = workdir / "output.bin"
        output_dir = workdir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        request_json.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        env = os.environ.copy()
        env["NEXUS_JOB_ID"] = f"{_service_name()}_{uuid.uuid4().hex}"
        env["NEXUS_ROUTE_KIND"] = _route_kind()
        env["NEXUS_REQUEST_JSON"] = str(request_json)
        env["NEXUS_OUTPUT_JSON"] = str(output_json)
        env["NEXUS_OUTPUT_MEDIA_PATH"] = str(output_media)
        env["NEXUS_OUTPUT_DIR"] = str(output_dir)
        proc = await asyncio.create_subprocess_exec(
            _env("NEXUS_SHELL", "/bin/sh"),
            "-lc",
            command,
            cwd=_env("NEXUS_WORKDIR", "/app"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=float(_int_env("NEXUS_TIMEOUT_SEC", 300)))
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail={"error": "runner timed out", "exception": str(exc)}) from exc
        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "runner failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )
        output: dict[str, Any] = {}
        if output_json.exists():
            output = json.loads(output_json.read_text(encoding="utf-8"))
        output.setdefault("_runner", {})
        output["_runner"]["stdout"] = (stdout_bytes or b"").decode(errors="ignore")[-2000:]
        if output_media.exists():
            output["_runner"]["output_media_path"] = str(output_media)
        return output


def _render_tts_output(output: dict[str, Any]) -> StreamingResponse | JSONResponse:
    if isinstance(output.get("response_json"), dict):
        return JSONResponse(output["response_json"])
    media_type = str(output.get("content_type") or _env("NEXUS_OUTPUT_MEDIA_TYPE", _route().get("media_type", "audio/wav")))
    if isinstance(output.get("audio_base64"), str):
        return StreamingResponse(iter([base64.b64decode(output["audio_base64"])]), media_type=media_type)
    if isinstance(output.get("audio_path"), str):
        path = Path(output["audio_path"])
        if path.exists():
            return StreamingResponse(iter([path.read_bytes()]), media_type=media_type)
    runner_media = output.get("_runner", {}).get("output_media_path")
    if isinstance(runner_media, str):
        path = Path(runner_media)
        if path.exists():
            return StreamingResponse(iter([path.read_bytes()]), media_type=media_type)
    raise HTTPException(status_code=502, detail="Runner produced no audio output")


app = FastAPI(title=_service_title(), version=_env("NEXUS_SERVICE_VERSION", "0.1.0"), description=_service_description())


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": _service_name(),
        "title": _service_title(),
        "route_kind": _route_kind(),
        "mode": _resolve_mode(),
        "port": _int_env("NEXUS_SERVICE_PORT", __PORT__),
        "endpoints": {
            "health": "/health",
            "healthz": "/healthz",
            "readyz": "/readyz",
            "models": "/v1/models",
            "metadata": "/v1/metadata",
            "capability": _route()["path"],
        },
    }


@app.get("/health")
@app.get("/healthz", include_in_schema=False)
def health() -> dict[str, Any]:
    return {"ok": True, "time": int(time.time()), "service": _service_name(), "mode": _resolve_mode()}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    error = await _runtime_error()
    if error is None:
        return JSONResponse(status_code=200, content={"ok": True, "service": _service_name(), "route_kind": _route_kind(), "mode": _resolve_mode()})
    return JSONResponse(status_code=503, content={"ok": False, "service": _service_name(), "route_kind": _route_kind(), "mode": _resolve_mode(), **error})


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": _env("NEXUS_MODEL_ID", "__MODEL_ID__"), "object": "model", "owned_by": _env("NEXUS_MODEL_OWNER", "nexus")}],
    }


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    return {
        "name": _service_name(),
        "title": _service_title(),
        "version": _env("NEXUS_SERVICE_VERSION", "0.1.0"),
        "description": _service_description(),
        "backend_class": _env("NEXUS_SERVICE_BACKEND_CLASS", "__SERVICE_NAME__"),
        "route_kind": _route_kind(),
        "mode": _resolve_mode(),
        "capabilities": _route()["capabilities"],
        "request_hint": _route()["hint"],
        "endpoints": {"health": "/health", "healthz": "/healthz", "readyz": "/readyz", "models": "/v1/models", "capability": _route()["path"]},
    }


@app.post(_route()["path"])
async def handle_capability(req: Request):
    try:
        body = await req.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Request body must be valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    _validate_body(body)
    mode = _resolve_mode()
    if mode == "upstream":
        return await _proxy_request(body)
    if mode == "command":
        output = await _run_command(body)
        if _route_kind() == "tts":
            return _render_tts_output(output)
        return JSONResponse(output)
    raise HTTPException(status_code=503, detail="Service is not configured")
