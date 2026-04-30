import asyncio
import json
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
import yaml


app = FastAPI(title="FollowYourCanvas Shim", version="0.1")


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
    return Path(__file__).with_name("run_followyourcanvas.py")


def _timeout_sec() -> int:
    return _int_env("FYC_TIMEOUT_SEC", 1800)


def _workdir() -> str:
    return _env("FYC_WORKDIR", "/data/app") or "/data/app"


def _model_id() -> str:
    return _env("FYC_MODEL_ID", "FollowYourCanvas") or "FollowYourCanvas"


def _default_config_path() -> Optional[Path]:
    raw = _env("FYC_DEFAULT_CONFIG")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(_workdir()) / path
    return path


def _resolve_runtime_path(value: str, *, workdir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return workdir / path


def _runtime_error() -> Optional[Dict[str, str]]:
    runner = _runner_script()
    workdir = Path(_workdir())
    if not runner.exists() or not workdir.exists():
        return {
            "reason": "missing_configuration",
            "detail": "FollowYourCanvas runner or workdir is missing inside the container.",
        }

    required_modules = ("torch", "diffusers", "transformers", "omegaconf", "decord", "segment_anything")
    workdir_text = str(workdir)
    if workdir_text not in sys.path:
        sys.path.insert(0, workdir_text)
    for module_name in required_modules:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            return {
                "reason": "missing_dependency",
                "detail": f"Required Python module {module_name!r} is unavailable: {type(exc).__name__}: {exc}",
            }

    config_path = _default_config_path()
    if config_path is None:
        return {
            "reason": "missing_default_config",
            "detail": "FYC_DEFAULT_CONFIG is not set; the service is not provisioned for prompt-only requests.",
        }
    if not config_path.exists():
        return {
            "reason": "missing_default_config",
            "detail": f"Configured FollowYourCanvas config is missing: {config_path}",
        }

    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {
            "reason": "invalid_default_config",
            "detail": f"Failed to parse {config_path}: {type(exc).__name__}: {exc}",
        }

    required_paths = [
        ("pretrained_model_path", False),
        ("motion_pretrained_model_path", False),
        ("lmm_path", False),
        ("image_pretrained_model_path", False),
        ("video_dir", False),
    ]
    for key, _allow_missing in required_paths:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            return {
                "reason": "invalid_default_config",
                "detail": f"Config {config_path} is missing required path setting {key!r}.",
            }
        if "YOUR_PATH" in value:
            return {
                "reason": "invalid_default_config",
                "detail": f"Config {config_path} still contains placeholder path for {key!r}.",
            }
        asset_path = _resolve_runtime_path(value, workdir=workdir)
        if not asset_path.exists():
            return {
                "reason": "missing_model_asset",
                "detail": f"Required FollowYourCanvas asset for {key!r} is missing: {asset_path}",
            }
    return None


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "time": _now(), "service": "followyourcanvas-shim"}


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
    return {"object": "list", "data": [{"id": _model_id(), "object": "model", "owned_by": "followyourcanvas"}]}


@app.post("/v1/videos/generations")
async def generate_video(payload: Dict[str, Any]) -> Any:
    error = _runtime_error()
    if error is not None:
        raise HTTPException(status_code=503, detail=error)
    runner = _runner_script()
    if not runner.exists():
        raise HTTPException(status_code=501, detail="FollowYourCanvas runner is not available in the container.")

    job_id = f"fyc_{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="fyc-") as tmpdir:
        workdir = Path(tmpdir)
        request_json_path = workdir / "request.json"
        request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        output_json_path = workdir / "output.json"
        output_dir = workdir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["FYC_JOB_ID"] = job_id
        env["FYC_REQUEST_JSON"] = str(request_json_path)
        env["FYC_OUTPUT_JSON"] = str(output_json_path)
        env["FYC_OUTPUT_DIR"] = str(output_dir)

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
            raise HTTPException(status_code=504, detail={"error": "followyourcanvas timed out", "job_id": job_id}) from exc

        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "followyourcanvas failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )

        if output_json_path.exists():
            return json.loads(output_json_path.read_text(encoding="utf-8"))
        return {"job_id": job_id, "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:]}
