import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="LuxTTS Shim", version="0.1")


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
    url = _env("LUXTTS_UPSTREAM_BASE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _upstream_endpoint() -> str:
    return _env("LUXTTS_UPSTREAM_ENDPOINT", "/v1/audio/speech") or "/v1/audio/speech"


def _run_command() -> Optional[str]:
    cmd = _env("LUXTTS_RUN_COMMAND")
    return cmd


def _shell_bin() -> str:
    shell = _env("LUXTTS_SHELL", "/bin/sh") or "/bin/sh"
    return shell


def _timeout_sec() -> int:
    return _int_env("LUXTTS_TIMEOUT_SEC", 120)


def _readyz_timeout_sec() -> int:
    return _int_env("LUXTTS_READYZ_TIMEOUT_SEC", 30)


def _workdir() -> str:
    return _env("LUXTTS_WORKDIR", "/var/lib/luxtts/app") or "/var/lib/luxtts/app"


def _resolve_workdir() -> str:
    candidates = [
        (_workdir() or "").strip(),
        "/app",
        "/",
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return "/"


async def _spawn_run_command(cmd: str, env: dict[str, str], cwd: str) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            _shell_bin(),
            "-c",
            cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        # launchd + uvloop on macOS can fail to exec the explicit shell path even when
        # the same command works interactively; retry through subprocess_shell.
        return await asyncio.create_subprocess_shell(
            cmd,
            executable=_shell_bin(),
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


def _model_id() -> str:
    return _env("LUXTTS_MODEL", "luxtts") or "luxtts"


def _output_format() -> str:
    return _env("LUXTTS_OUTPUT_FORMAT", "wav") or "wav"


def _media_type_for_output_format(fmt: Optional[str]) -> str:
    normalized = (fmt or "wav").strip().lower()
    if normalized == "mp3":
        return "audio/mpeg"
    if normalized == "ogg":
        return "audio/ogg"
    if normalized == "flac":
        return "audio/flac"
    if normalized == "webm":
        return "audio/webm"
    if normalized in {"m4a", "aac"}:
        return "audio/mp4"
    return "audio/wav"


def _readyz_input() -> str:
    return _env("LUXTTS_READYZ_INPUT", "readyz") or "readyz"


def _readyz_voice() -> str:
    return _env("LUXTTS_READYZ_VOICE", "en-us") or "en-us"


def _json_env(name: str) -> dict[str, Any]:
    raw = _env(name)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _refs_dir() -> str:
    return _env("LUXTTS_REFS_DIR", "/var/lib/luxtts/voices") or "/var/lib/luxtts/voices"


def _shared_refs_dir() -> str:
    return _env("LUXTTS_SHARED_REFS_DIR", "/var/lib/tts_refs") or "/var/lib/tts_refs"


def _sync_shared_refs_enabled() -> bool:
    raw = (_env("LUXTTS_SYNC_SHARED_REFS", "true") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _sync_overwrite_enabled() -> bool:
    raw = (_env("LUXTTS_SYNC_OVERWRITE", "false") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _sync_shared_refs_to_local() -> None:
    if not _sync_shared_refs_enabled():
        return
    source = _shared_refs_dir().strip()
    destination = _refs_dir().strip()
    if not source or not destination:
        return
    if not os.path.isdir(source):
        return
    os.makedirs(destination, exist_ok=True)
    allowed_exts = {".wav", ".mp3", ".ogg", ".weba", ".webm", ".flac", ".m4a", ".aac"}
    overwrite = _sync_overwrite_enabled()
    for name in os.listdir(source):
        src_path = os.path.join(source, name)
        if not os.path.isfile(src_path):
            continue
        _, ext = os.path.splitext(name)
        if ext.lower() not in allowed_exts:
            continue
        dst_path = os.path.join(destination, name)
        if os.path.exists(dst_path) and not overwrite:
            continue
        shutil.copy2(src_path, dst_path)


def _discover_ref_voices() -> list[str]:
    directory = _refs_dir().strip()
    if not directory or not os.path.isdir(directory):
        return []
    exts = {".wav", ".mp3", ".ogg", ".weba", ".webm", ".flac", ".m4a", ".aac"}
    out: list[str] = []
    seen: set[str] = set()
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in exts:
            continue
        voice = stem.strip().lower()
        if not voice or voice in seen:
            continue
        seen.add(voice)
        out.append(voice)
    out.sort()
    return out


@app.on_event("startup")
def startup_sync_refs() -> None:
    _sync_shared_refs_to_local()


def _voices() -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    def add(values: list[str]) -> None:
        for value in values:
            v = str(value or "").strip()
            if not v:
                continue
            k = v.lower()
            if k in seen:
                continue
            seen.add(k)
            merged.append(v)

    raw = _env("LUXTTS_VOICES")
    if raw:
        values = [item.strip() for item in raw.split(",") if item.strip()]
        add(values)

    map_keys = [str(k).strip() for k in _json_env("LUXTTS_VOICE_MAP_JSON").keys()]
    add(map_keys)
    add(_discover_ref_voices())

    if not merged:
        add(["prompt"])

    return merged


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "luxtts-shim"}


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
                "owned_by": "luxtts",
            }
        ],
    }


@app.get("/v1/voices")
def voices_v1() -> list[str]:
    _sync_shared_refs_to_local()
    return _voices()


@app.get("/voices")
def voices_compat() -> list[str]:
    _sync_shared_refs_to_local()
    return _voices()


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
            detail="LUXTTS_UPSTREAM_BASE_URL not set and LUXTTS_RUN_COMMAND not set; shim cannot synthesize audio.",
        )

    _sync_shared_refs_to_local()

    job_id = f"luxtts_{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="luxtts-") as tmpdir:
        workdir = Path(tmpdir)
        request_json_path = workdir / "request.json"
        output_path = workdir / f"output.{_output_format()}"
        request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["LUXTTS_JOB_ID"] = job_id
        env["LUXTTS_REQUEST_JSON"] = str(request_json_path)
        env["LUXTTS_OUTPUT_PATH"] = str(output_path)

        resolved_cwd = _resolve_workdir()
        try:
            proc = await _spawn_run_command(cmd, env, resolved_cwd)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "luxtts subprocess launch failed",
                    "detail": f"{type(e).__name__}: {e}",
                    "shell": _shell_bin(),
                    "cwd": resolved_cwd,
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
                    "error": "luxtts subprocess timed out",
                    "job_id": job_id,
                    "timeout_sec": _timeout_sec(),
                },
            )

        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "luxtts subprocess failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )

        if not output_path.exists():
            raise HTTPException(status_code=502, detail="LUXTTS_OUTPUT_PATH not written by subprocess.")
        if output_path.stat().st_size <= 0:
            raise HTTPException(status_code=502, detail="LUXTTS_OUTPUT_PATH is empty.")

        return StreamingResponse(output_path.open("rb"), media_type=_media_type_for_output_format(_output_format()))


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
        job_id = f"luxtts_readyz_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="luxtts-readyz-") as tmpdir:
            workdir = Path(tmpdir)
            request_json_path = workdir / "request.json"
            output_path = workdir / f"output.{_output_format()}"
            request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            env = os.environ.copy()
            env["LUXTTS_JOB_ID"] = job_id
            env["LUXTTS_REQUEST_JSON"] = str(request_json_path)
            env["LUXTTS_OUTPUT_PATH"] = str(output_path)
            env["LUXTTS_READYZ_PROBE"] = "1"

            resolved_cwd = _resolve_workdir()
            try:
                proc = await _spawn_run_command(cmd, env, resolved_cwd)
            except Exception as e:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "reason": "run_command_launch_failed",
                        "detail": f"{type(e).__name__}: {e}",
                        "shell": _shell_bin(),
                        "cwd": resolved_cwd,
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

            return JSONResponse(status_code=200, content={"ok": True, "mode": "run_command_probe"})

    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": "missing_configuration",
            "detail": "Set LUXTTS_UPSTREAM_BASE_URL or LUXTTS_RUN_COMMAND.",
        },
    )
