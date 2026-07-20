from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


ENGINE = (os.environ.get("NEXUS_MEDIA_ENGINE") or "video").strip().lower()
SERVICE_NAME = (os.environ.get("NEXUS_MEDIA_SERVICE_NAME") or f"{ENGINE}-video").strip()
MODEL_ID = (os.environ.get("NEXUS_MEDIA_MODEL_ID") or ENGINE).strip()
JOB_PREFIX = re.sub(r"[^a-z0-9]+", "_", ENGINE).strip("_") or "video"
_SAFE_JOB_RE = re.compile(rf"^{re.escape(JOB_PREFIX)}_[A-Fa-f0-9]{{32}}$")

app = FastAPI(title=f"{SERVICE_NAME} Shim", version="0.1")


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


def _now() -> int:
    return int(time.time())


def _output_root() -> Path:
    return Path(_env("MEDIA_OUTPUT_ROOT", "/data/outputs"))


def _runner_script() -> Path:
    return Path(__file__).with_name("run_video.py")


def _runner_python() -> str:
    return _env("MEDIA_RUNNER_PYTHON", sys.executable)


def _runner_python_available(command: str) -> bool:
    candidate = Path(command)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def _upstream_dir() -> Path:
    return Path(_env("MEDIA_UPSTREAM_DIR", "/data/app"))


def _safe_name(name: str) -> bool:
    return bool(name) and Path(name).name == name and "/" not in name and "\\" not in name


def _output_url(request: Request, job_id: str, name: str) -> str:
    configured = _env("MEDIA_PUBLIC_BASE_URL").rstrip("/")
    base = configured or str(request.base_url).rstrip("/")
    return f"{base}/outputs/{quote(job_id, safe='')}/{quote(name, safe='')}"


def _required_path_errors() -> list[str]:
    errors: list[str] = []
    runner = _runner_script()
    runner_python = _runner_python()
    upstream = _upstream_dir()
    if not runner.exists():
        errors.append(f"runner missing: {runner}")
    if not _runner_python_available(runner_python):
        errors.append(f"runner Python is not executable or PATH-resolvable: {runner_python}")
    if not upstream.exists():
        errors.append(f"upstream checkout missing: {upstream}")

    if ENGINE == "ltx":
        required = {
            "LTX_DISTILLED_CHECKPOINT_PATH": _env("LTX_DISTILLED_CHECKPOINT_PATH"),
            "LTX_SPATIAL_UPSAMPLER_PATH": _env("LTX_SPATIAL_UPSAMPLER_PATH"),
            "LTX_GEMMA_ROOT": _env("LTX_GEMMA_ROOT"),
        }
    elif ENGINE == "hunyuan":
        required = {"HUNYUAN_MODEL_PATH": _env("HUNYUAN_MODEL_PATH")}
    else:
        required = {}

    for key, raw_path in required.items():
        if not raw_path:
            errors.append(f"{key} is not configured")
        elif not Path(raw_path).exists():
            errors.append(f"{key} does not exist: {raw_path}")

    try:
        root = _output_root()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / f".write-check-{uuid.uuid4().hex}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        errors.append(f"output directory unavailable: {type(exc).__name__}: {exc}")
    return errors


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "time": _now(), "service": SERVICE_NAME, "engine": ENGINE}


@app.get("/readyz")
def readyz() -> JSONResponse:
    errors = _required_path_errors()
    if not errors:
        return JSONResponse(status_code=200, content={"ok": True, "engine": ENGINE, "model": MODEL_ID})
    return JSONResponse(
        status_code=503,
        content={"ok": False, "reason": "runtime_not_ready", "engine": ENGINE, "errors": errors},
    )


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": ENGINE}],
    }


@app.get("/outputs/{job_id}/{name}")
def get_output(job_id: str, name: str) -> FileResponse:
    if not _SAFE_JOB_RE.fullmatch(job_id) or not _safe_name(name):
        raise HTTPException(status_code=404, detail="output not found")
    root = _output_root().resolve()
    path = (root / job_id / name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(str(path), media_type="video/mp4")


@app.post("/v1/videos/generations")
async def generate_video(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    errors = _required_path_errors()
    if errors:
        raise HTTPException(
            status_code=503,
            detail={"error": "runtime_not_ready", "engine": ENGINE, "errors": errors},
        )
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    job_id = f"{JOB_PREFIX}_{uuid.uuid4().hex}"
    output_dir = _output_root() / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"

    with tempfile.TemporaryDirectory(prefix=f"{JOB_PREFIX}-") as tmpdir:
        request_path = Path(tmpdir) / "request.json"
        request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "MEDIA_REQUEST_JSON": str(request_path),
                "MEDIA_RESULT_JSON": str(result_path),
                "MEDIA_OUTPUT_DIR": str(output_dir),
                "MEDIA_JOB_ID": job_id,
            }
        )
        proc = await asyncio.create_subprocess_exec(
            _runner_python(),
            str(_runner_script()),
            cwd=str(_upstream_dir()),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout = max(60, _int_env("MEDIA_GENERATION_TIMEOUT_SEC", 3600))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise HTTPException(
                status_code=504,
                detail={"error": f"{ENGINE} generation timed out", "job_id": job_id},
            ) from exc

    stdout_text = (stdout or b"").decode(errors="ignore")
    stderr_text = (stderr or b"").decode(errors="ignore")
    if proc.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"{ENGINE} generation failed",
                "job_id": job_id,
                "returncode": proc.returncode,
                "stdout": stdout_text[-4000:],
                "stderr": stderr_text[-4000:],
            },
        )

    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        result = {"status": "ok", "videos": [item.name for item in output_dir.glob("*.mp4")]}
    videos = [str(item) for item in result.get("videos", []) if isinstance(item, str) and _safe_name(item)]
    urls = [_output_url(request, job_id, name) for name in videos]
    result.update({"job_id": job_id, "engine": ENGINE, "model": MODEL_ID, "urls": urls})
    if urls:
        result["url"] = urls[0]
        result["video_url"] = urls[0]
        result["data"] = [{"url": url} for url in urls]
    return result
