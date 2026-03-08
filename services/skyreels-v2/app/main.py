import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


app = FastAPI(title="SkyReels V2 Shim", version="0.1")


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


def _run_command() -> Optional[str]:
    return _env("SKYREELS_RUN_COMMAND")


def _timeout_sec() -> int:
    return _int_env("SKYREELS_TIMEOUT_SEC", 3600)


def _workdir() -> str:
    return _env("SKYREELS_WORKDIR", "/data/app") or "/data/app"


def _model_id() -> str:
    return _env("SKYREELS_MODEL_ID", "SkyReels-V2") or "SkyReels-V2"


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "skyreels-v2-shim"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    if _run_command():
        return JSONResponse(status_code=200, content={"ok": True})
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "reason": "missing_configuration",
            "detail": "Set SKYREELS_RUN_COMMAND to a runnable upstream invocation.",
        },
    )


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    return {"object": "list", "data": [{"id": _model_id(), "object": "model", "owned_by": "skyworkai"}]}


@app.post("/v1/videos/generations")
async def generate_video(payload: Dict[str, Any]) -> Any:
    cmd = _run_command()
    if not cmd:
        raise HTTPException(status_code=501, detail="SKYREELS_RUN_COMMAND is not configured.")

    job_id = f"skyreels_{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="skyreels-") as tmpdir:
        workdir = Path(tmpdir)
        request_json_path = workdir / "request.json"
        request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        output_json_path = workdir / "output.json"
        output_dir = workdir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SKYREELS_JOB_ID"] = job_id
        env["SKYREELS_REQUEST_JSON"] = str(request_json_path)
        env["SKYREELS_OUTPUT_JSON"] = str(output_json_path)
        env["SKYREELS_OUTPUT_DIR"] = str(output_dir)

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
            raise HTTPException(status_code=504, detail={"error": "skyreels timed out", "job_id": job_id}) from exc

        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "skyreels failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )

        if output_json_path.exists():
            return json.loads(output_json_path.read_text(encoding="utf-8"))
        return {"job_id": job_id, "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:]}