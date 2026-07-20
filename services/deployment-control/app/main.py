from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator


BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TERMINAL_STATES = {"succeeded", "failed"}
MAX_LOG_LINES = 300


def _csv_env(name: str, default: str) -> set[str]:
    return {item.strip() for item in os.getenv(name, default).split(",") if item.strip()}


def repo_root() -> Path:
    return Path(os.getenv("DEPLOY_CONTROL_REPO_ROOT", "/workspace")).resolve()


def state_dir() -> Path:
    return Path(
        os.getenv("DEPLOY_CONTROL_STATE_DIR", "/var/lib/deployment-control")
    ).resolve()


def allowed_hosts() -> set[str]:
    return _csv_env(
        "DEPLOY_CONTROL_ALLOWED_HOSTS",
        "ai2,ada2,meltdown,migraine,stackrot",
    )


def allowed_components() -> set[str]:
    return _csv_env(
        "DEPLOY_CONTROL_ALLOWED_COMPONENTS",
        (
            "deployment-control,etcd,followyourcanvas,gateway,heartmula,images,"
            "invokeai,lifecycle-manager,lighton-ocr,luxtts,mediamtx,mlx,nginx,"
            "personaplex,qwen3-tts,sdxl-turbo,skyreels-v2,telegram-bot,tts,"
            "vllm,vllm-embeddings,vllm-fast,vllm-meltdown,vllm-strong"
        ),
    )


def allowed_branches() -> set[str]:
    return _csv_env("DEPLOY_CONTROL_ALLOWED_BRANCHES", "main")


def topology_components(host: str) -> set[str]:
    path = repo_root() / "deploy" / "topology" / "production.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        hosts = payload.get("hosts") if isinstance(payload, dict) else None
        host_config = hosts.get(host) if isinstance(hosts, dict) else None
        components = host_config.get("components") if isinstance(host_config, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read deployment topology: {path}") from exc
    if not isinstance(components, list):
        raise RuntimeError(f"deployment topology has no component list for host: {host}")
    return {str(item).strip() for item in components if str(item).strip()}


def _read_required_file(env_name: str, default: str) -> str:
    path = Path(os.getenv(env_name, default))
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"required credential file is unavailable: {path}") from exc
    if not value:
        raise RuntimeError(f"required credential file is empty: {path}")
    return value


def _expected_token() -> str:
    return _read_required_file(
        "DEPLOY_CONTROL_TOKEN_FILE", "/run/secrets/deploy-control-token"
    )


async def require_auth(authorization: str | None = Header(default=None)) -> None:
    try:
        expected = _expected_token()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    scheme, _, supplied = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(
        supplied.strip(), expected
    ):
        raise HTTPException(status_code=401, detail="invalid deployment-control token")


class DeploymentRequest(BaseModel):
    host: str
    components: list[str] = Field(min_length=1, max_length=16)
    environment: Literal["prod"] = "prod"
    branch: str = "main"
    reason: str = Field(default="", max_length=500)
    requested_by: str = Field(default="agent", max_length=100)

    @field_validator("host", "branch", "reason", "requested_by", mode="before")
    @classmethod
    def strip_strings(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("components")
    @classmethod
    def normalize_components(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = str(raw or "").strip()
            if value and value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("at least one component is required")
        return normalized


class DeploymentJob(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    host: str
    components: list[str]
    environment: str
    branch: str
    reason: str
    requested_by: str
    created_at: float
    started_at: float = 0.0
    finished_at: float = 0.0
    return_code: int | None = None
    error: str = ""
    log_tail: list[str] = Field(default_factory=list)


_jobs: dict[str, DeploymentJob] = {}
_job_order: deque[str] = deque(maxlen=200)
_queue: asyncio.Queue[str] = asyncio.Queue()
_worker_task: asyncio.Task[None] | None = None


def _jobs_file() -> Path:
    return state_dir() / "jobs.json"


def _persist_jobs() -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = [
        _jobs[job_id].model_dump()
        for job_id in _job_order
        if job_id in _jobs
    ]
    target = _jobs_file()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def _load_jobs() -> None:
    path = _jobs_file()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, list):
        return
    for item in payload[-200:]:
        try:
            job = DeploymentJob.model_validate(item)
        except Exception:
            continue
        if job.status in {"queued", "running"}:
            job.status = "failed"
            job.error = "deployment controller restarted before the job completed"
            job.finished_at = time.time()
        _jobs[job.id] = job
        _job_order.append(job.id)


def _validate_request(request: DeploymentRequest) -> None:
    if request.host not in allowed_hosts():
        raise HTTPException(status_code=400, detail=f"host is not allowed: {request.host}")
    if request.branch not in allowed_branches() or not BRANCH_RE.fullmatch(
        request.branch
    ):
        raise HTTPException(
            status_code=400,
            detail=f"branch is not allowed: {request.branch}",
        )
    invalid = [
        component
        for component in request.components
        if component not in allowed_components()
        or not COMPONENT_RE.fullmatch(component)
    ]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"component is not allowed: {', '.join(invalid)}",
        )
    if os.getenv("DEPLOY_CONTROL_ENFORCE_TOPOLOGY", "true").strip().lower() == "true":
        try:
            placed = topology_components(request.host)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        misplaced = [item for item in request.components if item not in placed]
        if misplaced:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"component is not assigned to {request.host} in production topology: "
                    f"{', '.join(misplaced)}"
                ),
            )


def _deployment_command(job: DeploymentJob) -> list[str]:
    script = repo_root() / "deploy" / "scripts" / "remote-deploy.sh"
    if not script.is_file():
        raise RuntimeError(f"deployment script is unavailable: {script}")
    return [
        str(script),
        "--yes",
        "--components",
        ",".join(job.components),
        "--topology-host",
        job.host,
        job.environment,
        job.branch,
    ]


def _redact_log_line(line: str) -> str:
    value = line.rstrip()
    value = re.sub(
        r"(?i)(authorization|token|password|secret)(\s*[=:]\s*)\S+",
        r"\1\2[redacted]",
        value,
    )
    return value[:4000]


async def _run_job(job: DeploymentJob) -> None:
    job.status = "running"
    job.started_at = time.time()
    _persist_jobs()
    try:
        if os.getenv("DEPLOY_CONTROL_SYNC_REPO", "true").strip().lower() == "true":
            sync_process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo_root()),
                "pull",
                "--ff-only",
                "origin",
                job.branch,
                cwd=str(repo_root()),
                env=dict(os.environ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert sync_process.stdout is not None
            async for raw_line in sync_process.stdout:
                line = _redact_log_line(raw_line.decode("utf-8", errors="replace"))
                if line:
                    job.log_tail.append(line)
                    del job.log_tail[:-MAX_LOG_LINES]
            sync_return_code = await sync_process.wait()
            if sync_return_code != 0:
                raise RuntimeError(
                    f"controller repository sync exited with status {sync_return_code}"
                )
        command = _deployment_command(job)
        env = dict(os.environ)
        env.setdefault("NEXUS_TRUST_GENERATED_SOPS_OVERLAYS", "false")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(repo_root()),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = _redact_log_line(raw_line.decode("utf-8", errors="replace"))
            if line:
                job.log_tail.append(line)
                del job.log_tail[:-MAX_LOG_LINES]
        job.return_code = await process.wait()
        if job.return_code == 0:
            job.status = "succeeded"
        else:
            job.status = "failed"
            job.error = f"remote-deploy exited with status {job.return_code}"
    except Exception as exc:
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished_at = time.time()
        _persist_jobs()


async def _worker() -> None:
    while True:
        job_id = await _queue.get()
        try:
            job = _jobs.get(job_id)
            if job is not None:
                await _run_job(job)
        finally:
            _queue.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _worker_task
    state_dir().mkdir(parents=True, exist_ok=True)
    _expected_token()
    _load_jobs()
    _worker_task = asyncio.create_task(_worker())
    try:
        yield
    finally:
        if _worker_task is not None:
            _worker_task.cancel()
            try:
                await _worker_task
            except asyncio.CancelledError:
                pass
            _worker_task = None


app = FastAPI(title="Nexus Deployment Control", version="1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/capabilities", dependencies=[Depends(require_auth)])
async def capabilities() -> dict[str, Any]:
    return {
        "controller_host": os.getenv("DEPLOY_CONTROL_HOST", "copyfail"),
        "allowed_hosts": sorted(allowed_hosts()),
        "allowed_components": sorted(allowed_components()),
        "allowed_branches": sorted(allowed_branches()),
        "environments": ["prod"],
        "serialized": True,
    }


@app.post(
    "/v1/deployments",
    status_code=202,
    response_model=DeploymentJob,
    dependencies=[Depends(require_auth)],
)
async def create_deployment(request: DeploymentRequest) -> DeploymentJob:
    _validate_request(request)
    job = DeploymentJob(
        id=uuid.uuid4().hex,
        status="queued",
        host=request.host,
        components=request.components,
        environment=request.environment,
        branch=request.branch,
        reason=request.reason,
        requested_by=request.requested_by,
        created_at=time.time(),
    )
    _jobs[job.id] = job
    _job_order.append(job.id)
    _persist_jobs()
    await _queue.put(job.id)
    return job


@app.get(
    "/v1/deployments",
    response_model=list[DeploymentJob],
    dependencies=[Depends(require_auth)],
)
async def list_deployments(limit: int = 20) -> list[DeploymentJob]:
    bounded = max(1, min(limit, 100))
    ids = list(_job_order)[-bounded:]
    return [_jobs[job_id] for job_id in reversed(ids) if job_id in _jobs]


@app.get(
    "/v1/deployments/{job_id}",
    response_model=DeploymentJob,
    dependencies=[Depends(require_auth)],
)
async def get_deployment(job_id: str) -> DeploymentJob:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="deployment job not found")
    return job
