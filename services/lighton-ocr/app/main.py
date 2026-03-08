import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


app = FastAPI(title="LightOnOCR Shim", version="0.1")


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


def _host_port_base_url() -> str:
    host = _env("LIGHTON_OCR_HOST", "127.0.0.1")
    port = _env("LIGHTON_OCR_PORT", "9155")
    return f"http://{host}:{port}".rstrip("/")


def _upstream_base_url() -> Optional[str]:
    url = _env("LIGHTON_OCR_UPSTREAM_BASE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _upstream_endpoint() -> str:
    return _env("LIGHTON_OCR_UPSTREAM_ENDPOINT", "/v1/ocr") or "/v1/ocr"


def _run_command() -> Optional[str]:
    return _env("LIGHTON_OCR_RUN_COMMAND")


def _timeout_sec() -> int:
    return _int_env("LIGHTON_OCR_TIMEOUT_SEC", 120)


def _workdir() -> str:
    return _env("LIGHTON_OCR_WORKDIR", "/app") or "/app"


def _decode_image(payload: Dict[str, Any], workdir: Path) -> Optional[Path]:
    image_b64 = payload.get("image")
    if image_b64:
        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 image: {exc}") from exc
        image_path = workdir / "input.png"
        image_path.write_bytes(image_bytes)
        return image_path

    image_url = payload.get("image_url")
    if image_url:
        image_path = workdir / "input.url"
        image_path.write_text(str(image_url), encoding="utf-8")
        return image_path

    return None


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "lighton-ocr-shim"}


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    model_id = _env("LIGHTON_OCR_MODEL_ID", "lightonai/LightOnOCR-2-1B")
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "owned_by": "lightonai",
            }
        ],
    }


@app.post("/v1/ocr")
async def ocr(payload: Dict[str, Any]) -> Any:
    upstream = _upstream_base_url()
    if upstream:
        timeout = httpx.Timeout(connect=10.0, read=float(_timeout_sec()), write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{upstream}{_upstream_endpoint()}", json=payload)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=data)
            return data

    cmd = _run_command()
    if not cmd:
        raise HTTPException(
            status_code=501,
            detail="LIGHTON_OCR_UPSTREAM_BASE_URL not set and LIGHTON_OCR_RUN_COMMAND not set; shim cannot run OCR.",
        )

    job_id = f"ocr_{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="lighton-ocr-") as tmpdir:
        workdir = Path(tmpdir)
        request_json_path = workdir / "request.json"
        request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        input_path = _decode_image(payload, workdir)
        output_json_path = workdir / "output.json"

        env = os.environ.copy()
        env["LIGHTON_OCR_JOB_ID"] = job_id
        env["LIGHTON_OCR_REQUEST_JSON"] = str(request_json_path)
        env["LIGHTON_OCR_OUTPUT_JSON"] = str(output_json_path)
        if input_path:
            env["LIGHTON_OCR_INPUT_PATH"] = str(input_path)

        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-lc",
            cmd,
            cwd=_workdir(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=float(_timeout_sec()))
        except TimeoutError as exc:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "lighton-ocr subprocess timed out",
                    "job_id": job_id,
                    "timeout_sec": _timeout_sec(),
                },
            ) from exc

        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "lighton-ocr subprocess failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )

        if not output_json_path.exists():
            raise HTTPException(status_code=502, detail="LIGHTON_OCR_OUTPUT_JSON not written by subprocess.")
        return json.loads(output_json_path.read_text(encoding="utf-8"))


@app.get("/readyz")
def readyz() -> JSONResponse:
    if _upstream_base_url() or _run_command():
        return JSONResponse(status_code=200, content={"ok": True, "base_url": _host_port_base_url()})
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": "missing_configuration",
            "detail": "Set LIGHTON_OCR_UPSTREAM_BASE_URL or LIGHTON_OCR_RUN_COMMAND.",
        },
    )