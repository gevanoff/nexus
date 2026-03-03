import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="Qwen3-TTS Shim", version="0.1")


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
    url = _env("QWEN3_TTS_UPSTREAM_BASE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _upstream_endpoint() -> str:
    return _env("QWEN3_TTS_UPSTREAM_ENDPOINT", "/v1/audio/speech") or "/v1/audio/speech"


def _run_command() -> Optional[str]:
    cmd = _env("QWEN3_TTS_RUN_COMMAND")
    return cmd


def _shell_bin() -> str:
    shell = _env("QWEN3_TTS_SHELL", "/bin/sh") or "/bin/sh"
    return shell


def _timeout_sec() -> int:
    return _int_env("QWEN3_TTS_TIMEOUT_SEC", 120)


def _readyz_timeout_sec() -> int:
    return _int_env("QWEN3_TTS_READYZ_TIMEOUT_SEC", 20)


def _workdir() -> str:
    return _env("QWEN3_TTS_WORKDIR", "/var/lib/qwen3-tts/app") or "/var/lib/qwen3-tts/app"


def _model_id() -> str:
    return _env("QWEN3_TTS_MODEL", "qwen3-tts") or "qwen3-tts"


def _output_format() -> str:
    return _env("QWEN3_TTS_OUTPUT_FORMAT", "wav") or "wav"


def _readyz_input() -> str:
    return _env("QWEN3_TTS_READYZ_INPUT", "readyz") or "readyz"


def _readyz_voice() -> str:
    return _env("QWEN3_TTS_READYZ_VOICE", "alloy") or "alloy"


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "qwen3-tts-shim"}


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return health()


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": _model_id(),
                "object": "model",
                "owned_by": "qwen",
            }
        ],
    }


@app.post("/v1/audio/speech")
async def audio_speech(payload: Dict[str, Any]) -> Any:
    upstream = _upstream_base_url()
    if upstream:
        timeout = httpx.Timeout(connect=10.0, read=float(_timeout_sec()), write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{upstream}{_upstream_endpoint()}", json=payload)
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return StreamingResponse(resp.aiter_bytes(), media_type=resp.headers.get("content-type", "audio/wav"))

    cmd = _run_command()
    if not cmd:
        raise HTTPException(
            status_code=501,
            detail="QWEN3_TTS_UPSTREAM_BASE_URL not set and QWEN3_TTS_RUN_COMMAND not set; shim cannot synthesize audio.",
        )

    job_id = f"qwen3tts_{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="qwen3-tts-") as tmpdir:
        workdir = Path(tmpdir)
        request_json_path = workdir / "request.json"
        output_path = workdir / f"output.{_output_format()}"
        request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["QWEN3_TTS_JOB_ID"] = job_id
        env["QWEN3_TTS_REQUEST_JSON"] = str(request_json_path)
        env["QWEN3_TTS_OUTPUT_PATH"] = str(output_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                _shell_bin(),
                "-c",
                cmd,
                cwd=_workdir(),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "qwen3-tts subprocess launch failed",
                    "detail": f"{type(e).__name__}: {e}",
                    "shell": _shell_bin(),
                },
            )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=float(_timeout_sec()))
        except TimeoutError:
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
                    "error": "qwen3-tts subprocess timed out",
                    "job_id": job_id,
                    "timeout_sec": _timeout_sec(),
                },
            )

        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "qwen3-tts subprocess failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )

        if not output_path.exists():
            raise HTTPException(status_code=502, detail="QWEN3_TTS_OUTPUT_PATH not written by subprocess.")

        return StreamingResponse(output_path.open("rb"), media_type="audio/wav")


@app.get("/readyz")
async def readyz() -> JSONResponse:
    upstream = _upstream_base_url()
    if upstream:
        timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{upstream}/v1/models")
            if resp.status_code < 400:
                return JSONResponse(status_code=200, content={"ok": True, "mode": "upstream"})
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "reason": "upstream_probe_failed",
                    "detail": f"Upstream /v1/models returned {resp.status_code}",
                },
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "reason": "upstream_probe_failed",
                    "detail": f"Upstream probe error: {exc}",
                },
            )

    cmd = _run_command()
    if cmd:
        payload = {
            "model": _model_id(),
            "input": _readyz_input(),
            "voice": _readyz_voice(),
            "response_format": _output_format(),
        }
        job_id = f"qwen3tts_readyz_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="qwen3-tts-readyz-") as tmpdir:
            workdir = Path(tmpdir)
            request_json_path = workdir / "request.json"
            output_path = workdir / f"output.{_output_format()}"
            request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            env = os.environ.copy()
            env["QWEN3_TTS_JOB_ID"] = job_id
            env["QWEN3_TTS_REQUEST_JSON"] = str(request_json_path)
            env["QWEN3_TTS_OUTPUT_PATH"] = str(output_path)

            try:
                proc = await asyncio.create_subprocess_exec(
                    _shell_bin(),
                    "-c",
                    cmd,
                    cwd=_workdir(),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as e:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "reason": "run_command_launch_failed",
                        "detail": f"{type(e).__name__}: {e}",
                        "shell": _shell_bin(),
                    },
                )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(_readyz_timeout_sec()),
                )
            except TimeoutError:
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
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "reason": "run_command_timeout",
                        "detail": f"Readyz run_command timed out after {_readyz_timeout_sec()}s",
                    },
                )

            if proc.returncode != 0:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "reason": "run_command_failed",
                        "detail": {
                            "returncode": proc.returncode,
                            "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                            "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                        },
                    },
                )

            if not output_path.exists() or output_path.stat().st_size == 0:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "reason": "run_command_no_output",
                        "detail": "Readyz run_command did not produce output.",
                    },
                )

            return JSONResponse(status_code=200, content={"ok": True, "mode": "run_command"})

    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": "missing_configuration",
            "detail": "Set QWEN3_TTS_UPSTREAM_BASE_URL or QWEN3_TTS_RUN_COMMAND.",
        },
    )
