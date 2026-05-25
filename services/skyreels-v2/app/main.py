import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import importlib
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


app = FastAPI(title="SkyReels V2 Shim", version="0.1")
logger = logging.getLogger(__name__)
_SAFE_JOB_RE = re.compile(r"^skyreels_[A-Fa-f0-9]{32}$")
_CUDA_PROBE_CACHE: Dict[str, Any] = {"checked_at": 0.0, "error": None}


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


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
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


def _output_root() -> Path:
    return Path(_env("SKYREELS_OUTPUT_ROOT", "/data/outputs") or "/data/outputs")


def _cuda_probe_error() -> Optional[Dict[str, str]]:
    ttl_sec = max(0.0, _float_env("SKYREELS_CUDA_PROBE_CACHE_TTL_SEC", 30.0))
    now = time.monotonic()
    if ttl_sec > 0 and _CUDA_PROBE_CACHE["checked_at"] and now - float(_CUDA_PROBE_CACHE["checked_at"]) < ttl_sec:
        cached_error = _CUDA_PROBE_CACHE["error"]
        return cached_error if isinstance(cached_error, dict) else None

    code = "\n".join(
        [
            "import torch",
            "if not torch.cuda.is_available() or torch.cuda.device_count() <= 0:",
            "    raise SystemExit('cuda_unavailable')",
            "torch.cuda.init()",
        ]
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=max(1.0, _float_env("SKYREELS_CUDA_PROBE_TIMEOUT_SEC", 15.0)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        error: Optional[Dict[str, str]] = {
            "reason": "cuda_check_timeout",
            "detail": "Timed out while verifying CUDA availability in a child Python process.",
        }
    except Exception as exc:
        error = {
            "reason": "cuda_check_failed",
            "detail": f"Unable to verify CUDA availability: {type(exc).__name__}: {exc}",
        }
    else:
        if proc.returncode == 0:
            error = None
        else:
            stderr = (proc.stderr or proc.stdout or "").strip()
            detail = "SkyReels requires CUDA, but no CUDA GPU is visible inside a child Python process."
            if stderr:
                detail = f"{detail} Probe output: {stderr[-1000:]}"
            error = {
                "reason": "cuda_unavailable",
                "detail": detail,
            }

    _CUDA_PROBE_CACHE["checked_at"] = now
    _CUDA_PROBE_CACHE["error"] = error
    return error


def _writable_dir_error(path: Path) -> Optional[Dict[str, str]]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-check-{uuid.uuid4().hex}"
        try:
            probe.write_text("", encoding="utf-8")
        finally:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
    except Exception as exc:
        return {
            "reason": "output_dir_unavailable",
            "detail": f"SkyReels output directory is not writable: {type(exc).__name__}: {exc}",
        }
    return None


def _model_id() -> str:
    return _env("SKYREELS_MODEL_ID", "SkyReels-V2") or "SkyReels-V2"


def _is_safe_output_name(name: str) -> bool:
    return bool(name) and Path(name).name == name and "/" not in name and "\\" not in name


def _output_url(request: Request, job_id: str, name: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/outputs/{job_id}/{quote(name, safe='')}"


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

    cuda_error = _cuda_probe_error()
    if cuda_error is not None:
        return cuda_error

    if not (workdir / "generate_video.py").exists():
        return {
            "reason": "missing_upstream_clone",
            "detail": "SkyReels upstream sources are missing from the configured workdir.",
        }
    output_error = _writable_dir_error(_output_root())
    if output_error is not None:
        return output_error
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


@app.get("/outputs/{job_id}/{name}")
def get_output(job_id: str, name: str) -> FileResponse:
    if not _SAFE_JOB_RE.match(job_id):
        raise HTTPException(status_code=404, detail="output not found")
    if not _is_safe_output_name(name):
        raise HTTPException(status_code=404, detail="output not found")

    output_root = _output_root()
    path = output_root / job_id / name
    try:
        resolved_root = output_root.resolve()
        resolved_path = path.resolve()
        if resolved_root not in resolved_path.parents:
            raise HTTPException(status_code=404, detail="output not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="output not found")

    if not resolved_path.exists() or not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(str(resolved_path))


@app.post("/v1/videos/generations")
async def generate_video(payload: Dict[str, Any], request: Request) -> Any:
    error = _runtime_error()
    if error is not None:
        raise HTTPException(status_code=503, detail=error)
    runner = _runner_script()
    if not runner.exists():
        raise HTTPException(status_code=501, detail="SkyReels runner is not available in the container.")

    job_id = f"skyreels_{uuid.uuid4().hex}"
    output_dir = _output_root() / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skyreels-") as tmpdir:
        workdir = Path(tmpdir)
        request_json_path = workdir / "request.json"
        request_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        output_json_path = workdir / "output.json"

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
            videos = [
                str(item)
                for item in (result.get("videos") or [])
                if isinstance(item, str) and _is_safe_output_name(str(item))
            ]
            urls = [_output_url(request, job_id, name) for name in videos]
            result["job_id"] = job_id
            result["urls"] = urls
            if urls:
                result["url"] = urls[0]
                result["video_url"] = urls[0]
                result["data"] = [{"url": url} for url in urls]
            logger.info(
                "SkyReels job completed job_id=%s status=%s videos=%s",
                job_id,
                result.get("status"),
                result.get("videos"),
            )
            return result
        logger.info("SkyReels job completed without output metadata job_id=%s", job_id)
        return {"job_id": job_id, "stdout": stdout_text[-4000:]}
