from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


SERVICE_NAME = "ace-step"
MODEL_ID = (os.environ.get("ACE_STEP_DIT_MODEL") or "acestep-v15-xl-sft").strip()
LM_MODEL_ID = (os.environ.get("ACE_STEP_LM_MODEL") or "acestep-5Hz-lm-4B").strip()
_SAFE_JOB_RE = re.compile(r"^acestep_[A-Fa-f0-9]{32}$")
app = FastAPI(title="ACE-Step 1.5 Shim", version="0.1")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _coerce_int(
    value: Any,
    *,
    field: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if value is None or value == "":
        result = default
    elif isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    else:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{field} must be an integer") from exc
    if minimum is not None and result < minimum:
        raise HTTPException(status_code=400, detail=f"{field} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise HTTPException(status_code=400, detail=f"{field} must be at most {maximum}")
    return result


def _coerce_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise HTTPException(status_code=400, detail=f"{field} must be a boolean")


def _upstream_base() -> str:
    return _env("ACE_STEP_UPSTREAM_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def _output_root() -> Path:
    return Path(_env("MEDIA_OUTPUT_ROOT", "/data/outputs"))


def _safe_name(name: str) -> bool:
    return bool(name) and Path(name).name == name and "/" not in name and "\\" not in name


def _output_url(request: Request, job_id: str, name: str) -> str:
    configured = _env("MEDIA_PUBLIC_BASE_URL").rstrip("/")
    base = configured or str(request.base_url).rstrip("/")
    return f"{base}/outputs/{quote(job_id, safe='')}/{quote(name, safe='')}"


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _first_task_id(payload: Any) -> str:
    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        for key in ("task_id", "id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _first_audio_reference(payload: Any) -> str:
    extensions = (".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac")
    preferred_keys = ("audio_path", "output_path", "file_path", "path", "audio_url", "url")
    for item in _walk(payload):
        if isinstance(item, dict):
            for key in preferred_keys:
                value = item.get(key)
                if isinstance(value, str) and value.lower().split("?", 1)[0].endswith(extensions):
                    return value
        elif isinstance(item, str) and item.lower().split("?", 1)[0].endswith(extensions):
            return item
    return ""


def _task_status(payload: Any) -> int | None:
    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        value = item.get("status")
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if status in {0, 1, 2, 3, 4}:
            return status
    return None


def _release_payload(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or payload.get("style") or "").strip()
    lyrics = str(payload.get("lyrics") or "").strip()
    if not prompt and not lyrics:
        raise HTTPException(status_code=400, detail="prompt/style or lyrics is required")

    duration = _coerce_int(
        payload.get("audio_duration", payload.get("duration")),
        field="audio_duration",
        default=30,
        minimum=10,
        maximum=600,
    )
    batch_size = _coerce_int(
        payload.get("batch_size"),
        field="batch_size",
        default=1,
        minimum=1,
        maximum=4,
    )
    thinking = _coerce_bool(payload.get("thinking"), field="thinking", default=True)
    use_random_seed = _coerce_bool(
        payload.get("use_random_seed"),
        field="use_random_seed",
        default=payload.get("seed") is None,
    )

    request_payload: dict[str, Any] = {
        "prompt": prompt,
        "lyrics": lyrics,
        "audio_duration": duration,
        "model": str(payload.get("model") or MODEL_ID),
        "thinking": thinking,
        "use_random_seed": use_random_seed,
        "batch_size": batch_size,
        "audio_format": str(payload.get("audio_format") or "wav"),
    }
    passthrough = {
        "seed",
        "temperature",
        "top_p",
        "top_k",
        "bpm",
        "keyscale",
        "timesignature",
        "language",
        "instrumental",
        "vocal_language",
        "reference_audio",
        "reference_audio_strength",
        "repainting_start",
        "repainting_end",
    }
    for key in passthrough:
        if key in payload and payload[key] is not None:
            request_payload[key] = payload[key]
    tags = payload.get("tags")
    if tags:
        request_payload["tags"] = tags
    return request_payload


async def _query_task(client: httpx.AsyncClient, task_id: str) -> Any:
    variants = (
        {"task_id_list": [task_id]},
        {"task_ids": [task_id]},
        {"task_id": task_id},
    )
    last_response: httpx.Response | None = None
    for body in variants:
        response = await client.post(f"{_upstream_base()}/query_result", json=body)
        last_response = response
        if response.status_code < 400:
            return response.json()
        if response.status_code not in {400, 404, 422}:
            break
    assert last_response is not None
    raise HTTPException(
        status_code=502,
        detail={"error": "ACE-Step query_result failed", "body": last_response.text[-4000:]},
    )


async def _persist_audio(client: httpx.AsyncClient, reference: str, destination: Path) -> None:
    if reference.startswith(("http://", "https://")):
        response = await client.get(reference)
    else:
        response = await client.get(f"{_upstream_base()}/v1/audio", params={"path": reference})
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={"error": "ACE-Step audio fetch failed", "body": response.text[-2000:]},
        )
    destination.write_bytes(response.content)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "time": int(time.time()), "service": SERVICE_NAME}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    root = _output_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{_upstream_base()}/health")
        if response.status_code >= 400:
            raise RuntimeError(f"upstream health status {response.status_code}")
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "reason": "upstream_not_ready", "detail": str(exc)},
        )
    return JSONResponse(status_code=200, content={"ok": True, "model": MODEL_ID, "lm_model": LM_MODEL_ID})


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": MODEL_ID, "object": "model", "owned_by": "ace-step"},
            {"id": LM_MODEL_ID, "object": "model", "owned_by": "ace-step"},
        ],
    }


@app.get("/outputs/{job_id}/{name}")
def get_output(job_id: str, name: str) -> FileResponse:
    if not _SAFE_JOB_RE.fullmatch(job_id) or not _safe_name(name):
        raise HTTPException(status_code=404, detail="output not found")
    root = _output_root().resolve()
    path = (root / job_id / name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(str(path))


@app.post("/v1/music/generations")
async def generate_music(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    release_payload = _release_payload(payload)
    timeout = max(120.0, _float_env("MEDIA_GENERATION_TIMEOUT_SEC", 1800.0))
    poll_interval = max(0.5, _float_env("ACE_STEP_POLL_INTERVAL_SEC", 2.0))
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=120.0)) as client:
        response = await client.post(f"{_upstream_base()}/release_task", json=release_payload)
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail={"error": "ACE-Step release_task failed", "body": response.text[-4000:]},
            )
        release_result = response.json()
        task_id = _first_task_id(release_result)
        if not task_id:
            raise HTTPException(
                status_code=502,
                detail={"error": "ACE-Step returned no task id", "body": release_result},
            )

        deadline = time.monotonic() + timeout
        result: Any = release_result
        reference = ""
        while time.monotonic() < deadline:
            result = await _query_task(client, task_id)
            reference = _first_audio_reference(result)
            status = _task_status(result)
            if reference or status == 1:
                break
            if status in {2, 3, 4}:
                raise HTTPException(
                    status_code=502,
                    detail={"error": "ACE-Step generation failed", "task_id": task_id, "body": result},
                )
            await asyncio.sleep(poll_interval)
        else:
            raise HTTPException(status_code=504, detail={"error": "ACE-Step generation timed out", "task_id": task_id})

        reference = reference or _first_audio_reference(result)
        if not reference:
            raise HTTPException(
                status_code=502,
                detail={"error": "ACE-Step completed without an audio artifact", "task_id": task_id, "body": result},
            )
        job_id = f"acestep_{uuid.uuid4().hex}"
        output_dir = _output_root() / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(reference.split("?", 1)[0]).suffix.lower()
        if suffix not in {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac"}:
            suffix = ".wav"
        filename = f"music{suffix}"
        await _persist_audio(client, reference, output_dir / filename)

    audio_url = _output_url(request, job_id, filename)
    return {
        "status": "ok",
        "job_id": job_id,
        "task_id": task_id,
        "model": MODEL_ID,
        "lm_model": LM_MODEL_ID,
        "audio_url": audio_url,
        "url": audio_url,
        "data": [{"url": audio_url}],
    }
