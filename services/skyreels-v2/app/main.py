import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import uuid
import importlib
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


app = FastAPI(title="SkyReels V2 Shim", version="0.1")
logger = logging.getLogger(__name__)


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


def _runner_script() -> Path:
    return Path(__file__).with_name("run_skyreels.py")


def _timeout_sec() -> int:
    return _int_env("SKYREELS_TIMEOUT_SEC", 3600)


def _workdir() -> str:
    return _env("SKYREELS_WORKDIR", "/data/app") or "/data/app"


def _model_id() -> str:
    return _env("SKYREELS_MODEL_ID", "SkyReels-V2") or "SkyReels-V2"


def _runtime_error() -> Optional[Dict[str, str]]:
    runner = _runner_script()
    workdir = Path(_workdir())
    if not runner.exists() or not workdir.exists():
        return {
            "reason": "missing_configuration",
            "detail": "SkyReels runner or workdir is missing inside the container.",
        }

    required_modules = ("torch", "diffusers", "transformers", "decord", "einops", "moviepy", "safetensors")
    for module_name in required_modules:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            return {
                "reason": "missing_dependency",
                "detail": f"Required Python module {module_name!r} is unavailable: {type(exc).__name__}: {exc}",
            }

    if not (workdir / "generate_video.py").exists():
        return {
            "reason": "missing_upstream_clone",
            "detail": "SkyReels upstream sources are missing from the configured workdir.",
        }
    return None


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "skyreels-v2-shim"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    error = _runtime_error()
    if error is None:
        return JSONResponse(status_code=200, content={"ok": True})
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            **error,
        },
    )


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    return {"object": "list", "data": [{"id": _model_id(), "object": "model", "owned_by": "skyworkai"}]}


@app.post("/v1/videos/generations")
async def generate_video(payload: Dict[str, Any]) -> Any:
    error = _runtime_error()
    if error is not None:
        raise HTTPException(status_code=503, detail=error)
    runner = _runner_script()
    if not runner.exists():
        raise HTTPException(status_code=501, detail="SkyReels runner is not available in the container.")

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
            sys.executable,
            str(runner),
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
            logger.warning(
                "SkyReels job timed out job_id=%s timeout_sec=%s payload_keys=%s",
                job_id,
                _timeout_sec(),
                sorted(str(key) for key in payload.keys()),
            )
            raise HTTPException(status_code=504, detail={"error": "skyreels timed out", "job_id": job_id}) from exc

        stdout_text = (stdout_bytes or b"").decode(errors="ignore")
        stderr_text = (stderr_bytes or b"").decode(errors="ignore")
        if proc.returncode != 0:
            logger.warning(
                "SkyReels job failed job_id=%s returncode=%s stdout=%s stderr=%s",
                job_id,
                proc.returncode,
                stdout_text[-2000:],
                stderr_text[-2000:],
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "skyreels failed",
                    "returncode": proc.returncode,
                    "stdout": stdout_text[-4000:],
                    "stderr": stderr_text[-4000:],
                },
            )

        if output_json_path.exists():
            result = json.loads(output_json_path.read_text(encoding="utf-8"))
            logger.info(
                "SkyReels job completed job_id=%s status=%s videos=%s",
                job_id,
                result.get("status"),
                result.get("videos"),
            )
            return result
        logger.info("SkyReels job completed without output metadata job_id=%s", job_id)
        return {"job_id": job_id, "stdout": stdout_text[-4000:]}
