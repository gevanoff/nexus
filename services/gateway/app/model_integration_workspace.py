from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit


ROUTE_KIND_CHOICES = {"chat", "embeddings", "images", "tts", "ocr", "video", "music", "json"}
RUNTIME_CHOICES = {"auto", "mlx", "vllm", "transformers"}

DOCKERFILE_TEMPLATE = """ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    NEXUS_SERVICE_PORT=__PORT__

ARG EXTRA_APT_PACKAGES=""

RUN apt-get update -y \\
  && apt-get install -y --no-install-recommends bash ca-certificates curl ${EXTRA_APT_PACKAGES} \\
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY runner/ ./runner/

EXPOSE __PORT__

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
  CMD python -c "import os, urllib.request; port=os.environ.get('NEXUS_SERVICE_PORT', '__PORT__'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read(); print('ok')" || exit 1

CMD ["sh", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${NEXUS_SERVICE_PORT:-__PORT__}"]
"""

ENV_TEMPLATE = """NEXUS_SERVICE_NAME=__SERVICE_NAME__
NEXUS_SERVICE_TITLE=__SERVICE_TITLE__
NEXUS_SERVICE_DESCRIPTION=__SERVICE_DESCRIPTION__
NEXUS_SERVICE_VERSION=0.1.0
NEXUS_SERVICE_PORT=__PORT__
NEXUS_SERVICE_BACKEND_CLASS=__BACKEND_CLASS__

NEXUS_ROUTE_KIND=__ROUTE_KIND__
NEXUS_MODEL_ID=__MODEL_ID__
NEXUS_MODEL_OWNER=huggingface

# auto: prefer upstream if configured, otherwise local command
# upstream: always proxy NEXUS_UPSTREAM_BASE_URL
# command: always execute NEXUS_RUN_COMMAND
NEXUS_EXECUTION_MODE=__EXECUTION_MODE__

NEXUS_TIMEOUT_SEC=300
NEXUS_READYZ_TIMEOUT_SEC=10
NEXUS_WORKDIR=/app
NEXUS_SHELL=/bin/sh

NEXUS_UPSTREAM_BASE_URL=__UPSTREAM_BASE_URL__
NEXUS_UPSTREAM_ENDPOINT=
NEXUS_UPSTREAM_READY_PATHS=/readyz,/healthz,/health,/v1/models

NEXUS_RUN_COMMAND=python runner/run_model.py
NEXUS_RUN_READY_COMMAND=

NEXUS_OUTPUT_MEDIA_TYPE=
HF_MODEL_ID=__MODEL_ID__
HF_HOME=/var/lib/huggingface
HUGGINGFACE_HUB_TOKEN=
ETCD_URL=http://etcd:2379
"""

DOCKER_COMPOSE_TEMPLATE = """services:
  __SERVICE_NAME__:
    build:
      context: ./services/__SERVICE_NAME__
    container_name: nexus-__SERVICE_NAME__
    env_file:
      - ./services/__SERVICE_NAME__/.env.example
    ports:
      - "__PORT__:__PORT__"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import os, urllib.request; port=os.environ.get('NEXUS_SERVICE_PORT', '__PORT__'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read(); print('ok')"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks:
      - nexus

  __SERVICE_NAME___registrar:
    build:
      context: ./services/service-registrar
    container_name: nexus-__SERVICE_NAME__-registrar
    depends_on:
      - __SERVICE_NAME__
    environment:
      ETCD_URL: ${ETCD_URL:-http://etcd:2379}
      NEXUS_SERVICE_NAME: __SERVICE_NAME__
      NEXUS_SERVICE_BASE_URL: http://__SERVICE_NAME__:__PORT__
      NEXUS_SERVICE_BACKEND_CLASS: __BACKEND_CLASS__
      NEXUS_SERVICE_METADATA_URL: http://__SERVICE_NAME__:__PORT__/v1/metadata
      NEXUS_SERVICE_HEALTH_URLS: http://__SERVICE_NAME__:__PORT__/readyz,http://__SERVICE_NAME__:__PORT__/health
    restart: unless-stopped
    networks:
      - nexus

networks:
  nexus:
    external: true
"""

NEXUS_MODEL_SERVICE_TEMPLATE = """from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


ROUTES: dict[str, dict[str, Any]] = {
    "chat": {"path": "/v1/chat/completions", "capabilities": ["chat"], "hint": "messages"},
    "embeddings": {"path": "/v1/embeddings", "capabilities": ["embeddings"], "hint": "input"},
    "images": {"path": "/v1/images/generations", "capabilities": ["images"], "hint": "prompt"},
    "tts": {"path": "/v1/audio/speech", "capabilities": ["tts"], "hint": "input", "media_type": "audio/wav"},
    "ocr": {"path": "/v1/ocr", "capabilities": ["ocr"], "hint": "image or image_url"},
    "video": {"path": "/v1/videos/generations", "capabilities": ["video"], "hint": "prompt"},
    "music": {"path": "/v1/music/generations", "capabilities": ["music"], "hint": "prompt"},
    "json": {"path": "/v1/run", "capabilities": ["custom"], "hint": "arbitrary JSON payload"},
}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv_env(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.replace("\\n", ",").split(",") if item.strip()]


def _route_kind() -> str:
    route_kind = _env("NEXUS_ROUTE_KIND", "__ROUTE_KIND__").lower()
    if route_kind not in ROUTES:
        raise RuntimeError(f"Unsupported NEXUS_ROUTE_KIND: {route_kind}")
    return route_kind


def _route() -> dict[str, Any]:
    return ROUTES[_route_kind()]


def _service_name() -> str:
    return _env("NEXUS_SERVICE_NAME", "__SERVICE_NAME__")


def _service_title() -> str:
    return _env("NEXUS_SERVICE_TITLE", "__SERVICE_TITLE__")


def _service_description() -> str:
    return _env("NEXUS_SERVICE_DESCRIPTION", "__SERVICE_DESCRIPTION__")


def _resolve_mode() -> str:
    mode = _env("NEXUS_EXECUTION_MODE", "auto").lower()
    if mode in {"upstream", "command"}:
        return mode
    if _env("NEXUS_UPSTREAM_BASE_URL"):
        return "upstream"
    if _env("NEXUS_RUN_COMMAND"):
        return "command"
    return "unconfigured"


def _validate_body(body: dict[str, Any]) -> None:
    route_kind = _route_kind()
    if route_kind == "chat" and not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    if route_kind == "embeddings" and "input" not in body:
        raise HTTPException(status_code=400, detail="input is required")
    if route_kind in {"images", "video", "music"} and not str(body.get("prompt") or "").strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if route_kind == "tts" and not str(body.get("input") or body.get("text") or "").strip():
        raise HTTPException(status_code=400, detail="input is required")
    if route_kind == "ocr" and "image" not in body and "image_url" not in body:
        raise HTTPException(status_code=400, detail="image or image_url is required")


async def _runtime_error() -> dict[str, Any] | None:
    mode = _resolve_mode()
    if mode == "unconfigured":
        return {"reason": "missing_configuration", "detail": "Set NEXUS_UPSTREAM_BASE_URL or NEXUS_RUN_COMMAND."}
    if mode == "upstream":
        base = _env("NEXUS_UPSTREAM_BASE_URL")
        ready_paths = _csv_env("NEXUS_UPSTREAM_READY_PATHS", "/readyz,/healthz,/health,/v1/models")
        if _route()["path"] not in ready_paths:
            ready_paths.append(_route()["path"])
        timeout = httpx.Timeout(connect=5.0, read=float(_int_env("NEXUS_READYZ_TIMEOUT_SEC", 10)), write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for path in ready_paths:
                url = f"{base.rstrip('/')}{path if path.startswith('/') else '/' + path}"
                try:
                    response = await client.get(url)
                    if response.status_code < 400:
                        return None
                except Exception:
                    continue
        return {"reason": "upstream_unhealthy", "detail": f"Upstream readiness probes failed for {base}"}
    return None


async def _proxy_request(body: dict[str, Any]) -> StreamingResponse | JSONResponse | Any:
    base = _env("NEXUS_UPSTREAM_BASE_URL")
    if not base:
        raise HTTPException(status_code=503, detail="NEXUS_UPSTREAM_BASE_URL is not configured")
    endpoint = _env("NEXUS_UPSTREAM_ENDPOINT", _route()["path"])
    timeout = httpx.Timeout(connect=10.0, read=float(_int_env("NEXUS_TIMEOUT_SEC", 300)), write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base.rstrip('/')}{endpoint}", json=body)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise HTTPException(status_code=response.status_code, detail=detail)
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if _route_kind() == "tts" and not content_type.startswith("application/json"):
            media_type = content_type or _env("NEXUS_OUTPUT_MEDIA_TYPE", _route().get("media_type", "audio/wav"))
            return StreamingResponse(iter([response.content]), media_type=media_type)
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}


async def _run_command(body: dict[str, Any]) -> dict[str, Any]:
    command = _env("NEXUS_RUN_COMMAND")
    if not command:
        raise HTTPException(status_code=503, detail="NEXUS_RUN_COMMAND is not configured")
    with tempfile.TemporaryDirectory(prefix=f"{_service_name()}-") as tmpdir:
        workdir = Path(tmpdir)
        request_json = workdir / "request.json"
        output_json = workdir / "output.json"
        output_media = workdir / "output.bin"
        output_dir = workdir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        request_json.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        env = os.environ.copy()
        env["NEXUS_JOB_ID"] = f"{_service_name()}_{uuid.uuid4().hex}"
        env["NEXUS_ROUTE_KIND"] = _route_kind()
        env["NEXUS_REQUEST_JSON"] = str(request_json)
        env["NEXUS_OUTPUT_JSON"] = str(output_json)
        env["NEXUS_OUTPUT_MEDIA_PATH"] = str(output_media)
        env["NEXUS_OUTPUT_DIR"] = str(output_dir)
        proc = await asyncio.create_subprocess_exec(
            _env("NEXUS_SHELL", "/bin/sh"),
            "-lc",
            command,
            cwd=_env("NEXUS_WORKDIR", "/app"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "runner failed",
                    "returncode": proc.returncode,
                    "stdout": (stdout_bytes or b"").decode(errors="ignore")[-4000:],
                    "stderr": (stderr_bytes or b"").decode(errors="ignore")[-4000:],
                },
            )
        output: dict[str, Any] = {}
        if output_json.exists():
            output = json.loads(output_json.read_text(encoding="utf-8"))
        output.setdefault("_runner", {})
        output["_runner"]["stdout"] = (stdout_bytes or b"").decode(errors="ignore")[-2000:]
        if output_media.exists():
            output["_runner"]["output_media_path"] = str(output_media)
        return output


def _render_tts_output(output: dict[str, Any]) -> StreamingResponse | JSONResponse:
    if isinstance(output.get("response_json"), dict):
        return JSONResponse(output["response_json"])
    media_type = str(output.get("content_type") or _env("NEXUS_OUTPUT_MEDIA_TYPE", _route().get("media_type", "audio/wav")))
    if isinstance(output.get("audio_base64"), str):
        return StreamingResponse(iter([base64.b64decode(output["audio_base64"])]), media_type=media_type)
    if isinstance(output.get("audio_path"), str):
        path = Path(output["audio_path"])
        if path.exists():
            return StreamingResponse(iter([path.read_bytes()]), media_type=media_type)
    runner_media = output.get("_runner", {}).get("output_media_path")
    if isinstance(runner_media, str):
        path = Path(runner_media)
        if path.exists():
            return StreamingResponse(iter([path.read_bytes()]), media_type=media_type)
    raise HTTPException(status_code=502, detail="Runner produced no audio output")


app = FastAPI(title=_service_title(), version=_env("NEXUS_SERVICE_VERSION", "0.1.0"), description=_service_description())


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": _service_name(),
        "title": _service_title(),
        "route_kind": _route_kind(),
        "mode": _resolve_mode(),
        "port": _int_env("NEXUS_SERVICE_PORT", __PORT__),
        "endpoints": {
            "health": "/health",
            "healthz": "/healthz",
            "readyz": "/readyz",
            "models": "/v1/models",
            "metadata": "/v1/metadata",
            "capability": _route()["path"],
        },
    }


@app.get("/health")
@app.get("/healthz", include_in_schema=False)
def health() -> dict[str, Any]:
    return {"ok": True, "time": int(time.time()), "service": _service_name(), "mode": _resolve_mode()}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    error = await _runtime_error()
    if error is None:
        return JSONResponse(status_code=200, content={"ok": True, "service": _service_name(), "route_kind": _route_kind(), "mode": _resolve_mode()})
    return JSONResponse(status_code=503, content={"ok": False, "service": _service_name(), "route_kind": _route_kind(), "mode": _resolve_mode(), **error})


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": _env("NEXUS_MODEL_ID", "__MODEL_ID__"), "object": "model", "owned_by": _env("NEXUS_MODEL_OWNER", "nexus")}],
    }


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    return {
        "name": _service_name(),
        "title": _service_title(),
        "version": _env("NEXUS_SERVICE_VERSION", "0.1.0"),
        "description": _service_description(),
        "backend_class": _env("NEXUS_SERVICE_BACKEND_CLASS", "__BACKEND_CLASS__"),
        "route_kind": _route_kind(),
        "mode": _resolve_mode(),
        "capabilities": _route()["capabilities"],
        "request_hint": _route()["hint"],
        "endpoints": {"health": "/health", "healthz": "/healthz", "readyz": "/readyz", "models": "/v1/models", "capability": _route()["path"]},
    }


@app.post(_route()["path"])
async def handle_capability(req: Request):
    try:
        body = await req.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Request body must be valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    _validate_body(body)
    mode = _resolve_mode()
    if mode == "upstream":
        return await _proxy_request(body)
    if mode == "command":
        output = await _run_command(body)
        if _route_kind() == "tts":
            return _render_tts_output(output)
        return JSONResponse(output)
    raise HTTPException(status_code=503, detail="Service is not configured")
"""


def parse_model_reference(value: str) -> Dict[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("model is required")
    if raw.startswith(("https://", "http://")):
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if host not in {"huggingface.co", "www.huggingface.co"}:
            raise ValueError("model URL must be on huggingface.co")
        path_parts = [part for part in (parts.path or "").split("/") if part]
        if path_parts[:1] == ["models"]:
            path_parts = path_parts[1:]
        if len(path_parts) < 2:
            raise ValueError("model URL must include owner/model")
        model_id = f"{path_parts[0]}/{path_parts[1]}"
        return {"input": raw, "model_id": model_id, "source_url": f"https://huggingface.co/{model_id}"}
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", raw):
        raise ValueError("model must be a HuggingFace owner/model id or huggingface.co URL")
    return {"input": raw, "model_id": raw, "source_url": f"https://huggingface.co/{raw}"}


def fetch_model_metadata(model_id: str, *, timeout_sec: float = 10.0) -> Dict[str, Any]:
    url = f"https://huggingface.co/api/models/{quote(model_id, safe='/')}"
    req = urlrequest.Request(url, headers={"User-Agent": "nexus-model-integration/1.0"})
    with urlrequest.urlopen(req, timeout=timeout_sec) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    return payload if isinstance(payload, dict) else {}


def _slugify(value: str, *, default: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug or default)[:63].strip("-") or default


def _route_kind_from_metadata(metadata: Dict[str, Any], *, explicit: Optional[str] = None) -> str:
    choice = str(explicit or "").strip().lower()
    if choice:
        if choice not in ROUTE_KIND_CHOICES:
            raise ValueError("route_kind must be one of: chat, embeddings, images, tts, ocr, video, music, json")
        return choice
    pipeline = str(metadata.get("pipeline_tag") or "").strip().lower()
    tags = {str(item).strip().lower() for item in metadata.get("tags") or [] if str(item).strip()}
    if pipeline in {"feature-extraction", "sentence-similarity"} or {"embedding", "embeddings", "text-embeddings-inference"}.intersection(tags):
        return "embeddings"
    if pipeline in {"text-to-image", "image-to-image"}:
        return "images"
    if pipeline in {"text-to-speech", "text-to-audio", "audio-to-audio"}:
        return "tts"
    if pipeline in {"text-to-video", "image-to-video"}:
        return "video"
    if pipeline in {"text-to-music"}:
        return "music"
    if pipeline in {"document-question-answering", "visual-question-answering", "image-text-to-text"}:
        return "json"
    return "chat"


def _runtime_from_metadata(model_id: str, metadata: Dict[str, Any], route_kind: str, *, preferred_runtime: Optional[str] = None) -> str:
    preferred = str(preferred_runtime or "auto").strip().lower() or "auto"
    if preferred not in RUNTIME_CHOICES:
        raise ValueError("preferred_runtime must be one of: auto, mlx, vllm, transformers")
    if preferred != "auto":
        return preferred
    tags = {str(item).strip().lower() for item in metadata.get("tags") or [] if str(item).strip()}
    library_name = str(metadata.get("library_name") or "").strip().lower()
    haystack = " ".join([model_id.lower(), library_name, *sorted(tags)])
    if "mlx" in haystack:
        return "mlx"
    if route_kind in {"chat", "embeddings"}:
        return "vllm"
    return "transformers"


def _safe_metadata_subset(metadata: Dict[str, Any]) -> Dict[str, Any]:
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    siblings = metadata.get("siblings") if isinstance(metadata.get("siblings"), list) else []
    return {
        "id": metadata.get("id") or "",
        "library_name": metadata.get("library_name") or "",
        "pipeline_tag": metadata.get("pipeline_tag") or "",
        "private": bool(metadata.get("private")),
        "gated": bool(metadata.get("gated")),
        "downloads": metadata.get("downloads"),
        "likes": metadata.get("likes"),
        "tags": [str(item) for item in metadata.get("tags") or [] if str(item).strip()][:40],
        "architectures": list(config.get("architectures") or [])[:10],
        "model_type": config.get("model_type") or "",
        "sample_files": [str(item.get("rfilename") or "") for item in siblings[:20] if isinstance(item, dict)],
    }


def build_integration_plan(
    *,
    model: str,
    preferred_runtime: Optional[str] = None,
    route_kind: Optional[str] = None,
    service_name: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    parsed = parse_model_reference(model)
    warnings: list[str] = []
    metadata: Dict[str, Any] = {}
    try:
        metadata = fetch_model_metadata(parsed["model_id"])
    except urlerror.HTTPError as exc:
        warnings.append(f"metadata fetch failed with HTTP {exc.code}; scaffolding from model id only")
    except Exception as exc:
        warnings.append(f"metadata fetch failed: {type(exc).__name__}: {exc}")

    selected_route = _route_kind_from_metadata(metadata, explicit=route_kind)
    runtime = _runtime_from_metadata(parsed["model_id"], metadata, selected_route, preferred_runtime=preferred_runtime)
    normalized_service_name = _slugify(service_name or f"hf-{parsed['model_id'].replace('/', '-')}", default="hf-model-adapter")
    backend_class = normalized_service_name.replace("-", "_")
    model_tail = parsed["model_id"].split("/", 1)[-1]
    display_name = f"HF {model_tail}"
    containerize = runtime != "mlx"
    shim_required = runtime == "transformers"
    execution_mode = "command" if shim_required else "upstream"
    upstream_base_url = "http://127.0.0.1:8000" if execution_mode == "upstream" else ""
    task_prompt = (
        f"Integrate the HuggingFace model {parsed['model_id']} into Nexus as a {selected_route} backend. "
        f"Use the generated workspace scaffold. Runtime strategy: {runtime}. "
        f"Containerize the adapter if appropriate ({'yes' if containerize else 'no'}). "
        f"Provide an industry-standard API surface compatible with OpenAI-style {selected_route} access. "
        f"Update README, env, compose or host-native launch files, backend registration snippets, and the implementation stub so the workspace is ready for Nexus integration."
    )
    extra_prompt = str(prompt or "").strip()
    if extra_prompt:
        task_prompt = f"{task_prompt}\n\nAdditional user guidance:\n{extra_prompt}"
    return {
        "model_input": parsed["input"],
        "model_id": parsed["model_id"],
        "source_url": parsed["source_url"],
        "route_kind": selected_route,
        "runtime": runtime,
        "containerize": containerize,
        "shim_required": shim_required,
        "execution_mode": execution_mode,
        "upstream_base_url": upstream_base_url,
        "service_name": normalized_service_name,
        "backend_class": backend_class,
        "display_name": display_name,
        "api_path": {
            "chat": "/v1/chat/completions",
            "embeddings": "/v1/embeddings",
            "images": "/v1/images/generations",
            "tts": "/v1/audio/speech",
            "ocr": "/v1/ocr",
            "video": "/v1/videos/generations",
            "music": "/v1/music/generations",
            "json": "/v1/run",
        }[selected_route],
        "hf_metadata": _safe_metadata_subset(metadata),
        "warnings": warnings,
        "prompt": task_prompt,
    }


def _render(template: str, replacements: Dict[str, str]) -> str:
    out = template
    for old, new in replacements.items():
        out = out.replace(old, new)
    return out


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scaffold_workspace(repo_root: Path, plan: Dict[str, Any]) -> list[str]:
    repo_root = Path(repo_root).resolve()
    replacements = {
        "__PORT__": "8610",
        "__SERVICE_NAME__": str(plan["service_name"]),
        "__SERVICE_TITLE__": str(plan["display_name"]),
        "__SERVICE_DESCRIPTION__": f"HuggingFace model adapter for {plan['model_id']}",
        "__BACKEND_CLASS__": str(plan["backend_class"]),
        "__ROUTE_KIND__": str(plan["route_kind"]),
        "__MODEL_ID__": str(plan["model_id"]),
        "__EXECUTION_MODE__": str(plan["execution_mode"]),
        "__UPSTREAM_BASE_URL__": str(plan["upstream_base_url"]),
    }
    created: list[str] = []

    _write(repo_root / ".gitignore", "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n.venv/\nvenv/\ndist/\nbuild/\n*.pyc\n*.pyo\n*.pyd\noutputs/\n.runtime/\n")
    created.append(str(repo_root / ".gitignore"))

    warnings = "\n".join(f"- {item}" for item in plan.get("warnings") or []) or "- none"
    root_readme = (
        f"# {plan['display_name']} Nexus Integration Workspace\n\n"
        f"This coding workspace was generated for integrating the HuggingFace model `{plan['model_id']}` into Nexus.\n\n"
        "## Summary\n\n"
        f"- Source: {plan['source_url']}\n"
        f"- Route kind: `{plan['route_kind']}`\n"
        f"- Runtime strategy: `{plan['runtime']}`\n"
        f"- Containerize: `{str(bool(plan['containerize'])).lower()}`\n"
        f"- Shim required: `{str(bool(plan['shim_required'])).lower()}`\n"
        f"- Service name: `{plan['service_name']}`\n"
        f"- Backend class: `{plan['backend_class']}`\n"
        f"- Target API path: `{plan['api_path']}`\n\n"
        "## Metadata Notes\n\n"
        f"- library: `{plan['hf_metadata'].get('library_name') or 'unknown'}`\n"
        f"- pipeline: `{plan['hf_metadata'].get('pipeline_tag') or 'unknown'}`\n"
        f"- gated: `{str(bool(plan['hf_metadata'].get('gated'))).lower()}`\n"
        f"- private: `{str(bool(plan['hf_metadata'].get('private'))).lower()}`\n\n"
        f"Warnings:\n{warnings}\n"
    )
    _write(repo_root / "README.md", root_readme)
    created.append(str(repo_root / "README.md"))

    agent_task = (
        "# Coding Agent Task\n\n"
        f"Goal:\n\n{plan['prompt']}\n\n"
        "Constraints:\n\n"
        "1. Reuse the generated scaffold instead of replacing it wholesale.\n"
        f"2. Keep the backend API compatible with `{plan['api_path']}`.\n"
        "3. If runtime is `mlx`, keep the integration host-native and do not add Docker/Compose as the primary runtime path.\n"
        "4. If runtime is not `mlx`, provide a containerized path and keep the health/model metadata endpoints consistent with Nexus patterns.\n"
        "5. Update `integration/backend-config-snippet.yaml` and `integration/lifecycle.backend.json` so operators can wire the backend into Nexus.\n"
        "6. Document blockers for gated weights, unsupported architectures, or missing runtime features in the README.\n"
    )
    _write(repo_root / "AGENT_TASK.md", agent_task)
    created.append(str(repo_root / "AGENT_TASK.md"))

    _write(repo_root / "integration_request.json", json.dumps(plan, indent=2, sort_keys=True) + "\n")
    created.append(str(repo_root / "integration_request.json"))

    capability = str(plan["route_kind"])
    health_path = "/v1/models" if capability in {"chat", "embeddings"} and not bool(plan["shim_required"]) else "/readyz"
    provider = "mlx" if plan["runtime"] == "mlx" else ("vllm" if plan["runtime"] == "vllm" else "shim")
    env_name = f"{str(plan['backend_class']).upper()}_BASE_URL"
    backend_snippet = (
        "backends:\n"
        f"  {plan['backend_class']}:\n"
        f"    class: {plan['backend_class']}\n"
        f"    provider: {provider}\n"
        f"    base_url: ${{{env_name}}}\n"
        f"    description: {plan['display_name']} ({plan['runtime']})\n"
        "    supported_capabilities:\n"
        f"      - {capability}\n"
        "    concurrency_limits:\n"
        f"      {capability}: 1\n"
        "    health:\n"
        f"      liveness: {health_path}\n"
        f"      readiness: {health_path}\n"
        "    payload_policy: {}\n"
    )
    _write(repo_root / "integration" / "backend-config-snippet.yaml", backend_snippet)
    created.append(str(repo_root / "integration" / "backend-config-snippet.yaml"))

    lifecycle = {
        str(plan["backend_class"]): {
            "display_name": plan["display_name"],
            "host": "set-me",
            "component": plan["service_name"],
            "tier": "optional",
            "capabilities": [plan["route_kind"]],
            "estimated_vram_mb": 0,
            "auto_start": True,
            "auto_stop": True,
            "compose_file": f"docker-compose.{plan['service_name']}.yml" if bool(plan["containerize"]) else "host-native",
            "ready_path": "/readyz" if bool(plan["shim_required"]) or bool(plan["containerize"]) else "/v1/models",
            "notes": f"Generated from HuggingFace model {plan['model_id']}. Fill in host, VRAM, secrets, and artifact requirements.",
        }
    }
    _write(repo_root / "integration" / "lifecycle.backend.json", json.dumps(lifecycle, indent=2, sort_keys=True) + "\n")
    created.append(str(repo_root / "integration" / "lifecycle.backend.json"))

    if bool(plan["containerize"]):
        service_root = repo_root / "services" / str(plan["service_name"])
        _write(service_root / "README.md", f"# {plan['service_name']}\n\nGenerated backend scaffold for HuggingFace model `{plan['model_id']}`.\n")
        _write(service_root / "Dockerfile", _render(DOCKERFILE_TEMPLATE, replacements))
        requirements = [
            "fastapi>=0.115,<1.0",
            "httpx>=0.27,<1.0",
            "uvicorn[standard]>=0.32,<1.0",
        ]
        if plan["runtime"] == "vllm":
            requirements.append("vllm>=0.8,<1.0")
        if plan["runtime"] == "transformers":
            requirements.extend(["transformers>=4.52,<5.0", "huggingface_hub>=0.23,<1.0", "torch>=2.3,<3.0"])
        _write(service_root / "requirements.txt", "\n".join(requirements) + "\n")
        _write(service_root / ".env.example", _render(ENV_TEMPLATE, replacements))
        _write(service_root / f"docker-compose.{plan['service_name']}.yml", _render(DOCKER_COMPOSE_TEMPLATE, replacements))
        _write(service_root / "lifecycle.backend.json", json.dumps(lifecycle, indent=2, sort_keys=True) + "\n")
        _write(service_root / "app" / "__init__.py", "")
        _write(service_root / "app" / "main.py", "from app.nexus_model_service import app\n")
        _write(service_root / "app" / "nexus_model_service.py", _render(NEXUS_MODEL_SERVICE_TEMPLATE, replacements))
        _write(service_root / "runner" / "README.md", "Implement the runtime-specific runner here.\n")
        _write(
            service_root / "runner" / "run_model.py",
            (
                "from __future__ import annotations\n\n"
                "import json\n"
                "import os\n"
                "from pathlib import Path\n\n"
                "def main() -> int:\n"
                "    request_path = Path(os.environ['NEXUS_REQUEST_JSON'])\n"
                "    output_path = Path(os.environ['NEXUS_OUTPUT_JSON'])\n"
                "    request_body = json.loads(request_path.read_text(encoding='utf-8'))\n"
                f"    response = {{'model': os.environ.get('HF_MODEL_ID', '{plan['model_id']}'), '_todo': {{'message': 'Replace this placeholder runner with real inference logic.', 'request_keys': sorted(request_body.keys())}}}}\n"
                "    output_path.write_text(json.dumps(response, indent=2), encoding='utf-8')\n"
                "    return 0\n\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(main())\n"
            ),
        )
        created.extend(
            [
                str(service_root / "README.md"),
                str(service_root / "Dockerfile"),
                str(service_root / "requirements.txt"),
                str(service_root / ".env.example"),
                str(service_root / f"docker-compose.{plan['service_name']}.yml"),
                str(service_root / "lifecycle.backend.json"),
                str(service_root / "app" / "__init__.py"),
                str(service_root / "app" / "main.py"),
                str(service_root / "app" / "nexus_model_service.py"),
                str(service_root / "runner" / "README.md"),
                str(service_root / "runner" / "run_model.py"),
            ]
        )
    else:
        host_root = repo_root / "host_native" / str(plan["service_name"])
        _write(host_root / "README.md", f"# Host-native MLX Integration\n\nUse the target host's MLX serving path for `{plan['model_id']}`.\n")
        _write(host_root / "model.env.example", f"HF_MODEL_ID={plan['model_id']}\nMLX_MODEL_ID={plan['model_id']}\nMLX_BASE_URL=http://127.0.0.1:10240\n")
        _write(host_root / "run-model.sh", f"#!/usr/bin/env bash\nset -eu\nMODEL_ID=\"${{HF_MODEL_ID:-{plan['model_id']}}}\"\necho \"TODO: launch MLX serving for $MODEL_ID with OpenAI-compatible routing\"\n")
        created.extend(
            [
                str(host_root / "README.md"),
                str(host_root / "model.env.example"),
                str(host_root / "run-model.sh"),
            ]
        )
    return created