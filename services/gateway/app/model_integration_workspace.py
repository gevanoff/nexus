from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit


ROUTE_KIND_CHOICES = {"chat", "embeddings", "images", "tts", "ocr", "video", "music", "json"}
RUNTIME_CHOICES = {"auto", "mlx", "vllm", "transformers"}
_VLLM_SUPPORTED_MODEL_TYPES = {
    "bloom",
    "dbrx",
    "deepseek",
    "deepseek_v2",
    "deepseek_v3",
    "exaone",
    "falcon",
    "gemma",
    "gemma2",
    "glm",
    "gpt_neox",
    "gptj",
    "llama",
    "mistral",
    "mixtral",
    "phi",
    "phi3",
    "phi4",
    "qwen2",
    "qwen2_moe",
    "qwen3",
}
_TEXT_GENERATION_PIPELINES = {"", "conversational", "text-generation"}
_VLLM_UNSUPPORTED_MARKERS = {
    "audio",
    "bart",
    "blip",
    "clip",
    "diffusers",
    "donut",
    "florence",
    "ggml",
    "gguf",
    "image-text-to-text",
    "image-to-text",
    "llama.cpp",
    "llava",
    "mllama",
    "mt5",
    "musicgen",
    "onnx",
    "onnxruntime",
    "pegasus",
    "reranker",
    "roberta",
    "sam",
    "seq2seq",
    "siglip",
    "speech",
    "stable-diffusion",
    "t5",
    "vision",
    "wav2vec",
    "whisper",
}
_FALLBACK_HOSTS = {
    "ai1": {
        "description": "Dual-GPU Linux/NVIDIA node (2x RTX 3090 24GB) for media ingress and secondary vLLM/CUDA capacity.",
        "platform": "linux",
        "resource_kind": "linux_nvidia",
    },
    "ai2": {
        "description": "Mac M3 Ultra with 512GB unified memory for gateway/control plane, containerized TTS, and host-native MLX reasoning.",
        "platform": "macos",
        "resource_kind": "macos",
    },
    "ada2": {
        "description": "Linux/NVIDIA node with 128GB system RAM and an RTX 6000 Ada 48GB for primary heavy vLLM plus image and video generation services.",
        "platform": "linux",
        "resource_kind": "linux_nvidia",
    },
    "meltdown": {
        "description": "Ubuntu 22.04 Linux/NVIDIA node with about 47GB system RAM and a GeForce RTX 5060 Ti 16GB for lighter CUDA overflow and staging.",
        "platform": "linux",
        "resource_kind": "linux_nvidia",
    },
}
_FALLBACK_BACKENDS = {
    "local_mlx": {
        "display_name": "MLX",
        "host": "ai2",
        "estimated_vram_mb": 0,
        "compose_managed": False,
        "ready_path": "/models",
    },
    "local_vllm": {
        "display_name": "vLLM Strong",
        "host": "ada2",
        "estimated_vram_mb": 28000,
        "compose_file": "docker-compose.vllm-strong.yml",
        "ready_path": "/models",
        "notes": "Comparable heavy-text lane on the RTX 6000 Ada host.",
    },
    "local_vllm_fast": {
        "display_name": "vLLM Fast",
        "host": "ai1",
        "estimated_vram_mb": 22000,
        "compose_file": "docker-compose.vllm-fast.yml",
        "ready_path": "/models",
    },
    "local_vllm_embeddings": {
        "display_name": "vLLM Embeddings",
        "host": "meltdown",
        "estimated_vram_mb": 12000,
        "compose_file": "docker-compose.vllm-embeddings.yml",
        "ready_path": "/models",
    },
    "gpu_fast": {
        "display_name": "SDXL-Turbo",
        "host": "meltdown",
        "estimated_vram_mb": 7000,
        "compose_file": "docker-compose.sdxl-turbo.yml",
        "ready_path": "/readyz",
    },
    "gpu_heavy": {
        "display_name": "InvokeAI Images",
        "host": "ada2",
        "estimated_vram_mb": 12000,
        "compose_file": "docker-compose.invokeai.yml",
        "ready_path": "/readyz",
    },
    "lighton_ocr": {
        "display_name": "LightOn OCR",
        "host": "ada2",
        "estimated_vram_mb": 7000,
        "compose_file": "docker-compose.lighton-ocr.yml",
        "ready_path": "/readyz",
    },
    "skyreels_v2": {
        "display_name": "SkyReels V2",
        "host": "ada2",
        "estimated_vram_mb": 18000,
        "compose_file": "docker-compose.skyreels-v2.yml",
        "ready_path": "/readyz",
    },
}

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def _load_topology_manifest() -> Dict[str, Any]:
    path = _repo_root() / "deploy" / "topology" / "production.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=1)
def _load_backend_lifecycle() -> Dict[str, Any]:
    path = _repo_root() / "deploy" / "topology" / "backend_lifecycle.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _host_profile(host: str) -> Dict[str, Any]:
    topology = _load_topology_manifest()
    topology_hosts = topology.get("hosts") if isinstance(topology.get("hosts"), dict) else {}
    host_data = topology_hosts.get(host) if isinstance(topology_hosts, dict) and isinstance(topology_hosts.get(host), dict) else {}
    lifecycle = _load_backend_lifecycle()
    lifecycle_hosts = lifecycle.get("hosts") if isinstance(lifecycle.get("hosts"), dict) else {}
    lifecycle_host = lifecycle_hosts.get(host) if isinstance(lifecycle_hosts, dict) and isinstance(lifecycle_hosts.get(host), dict) else {}
    fallback_host = _FALLBACK_HOSTS.get(host) if isinstance(_FALLBACK_HOSTS.get(host), dict) else {}
    return {
        "host": host,
        "description": str(host_data.get("description") or fallback_host.get("description") or "").strip(),
        "platform": str(host_data.get("platform") or fallback_host.get("platform") or "").strip(),
        "resource_kind": str(lifecycle_host.get("resource_kind") or fallback_host.get("resource_kind") or "").strip(),
    }


def _backend_profile(name: str) -> Dict[str, Any]:
    lifecycle = _load_backend_lifecycle()
    backends = lifecycle.get("backends") if isinstance(lifecycle.get("backends"), dict) else {}
    backend = backends.get(name) if isinstance(backends, dict) and isinstance(backends.get(name), dict) else {}
    fallback_backend = _FALLBACK_BACKENDS.get(name) if isinstance(_FALLBACK_BACKENDS.get(name), dict) else {}
    host = str(backend.get("host") or "").strip()
    if not host:
        host = str(fallback_backend.get("host") or "").strip()
    host_profile = _host_profile(host) if host else {}
    return {
        "name": name,
        "display_name": str(backend.get("display_name") or fallback_backend.get("display_name") or name).strip(),
        "host": host,
        "host_description": host_profile.get("description") or "",
        "platform": host_profile.get("platform") or "",
        "resource_kind": host_profile.get("resource_kind") or "",
        "estimated_vram_mb": int(backend.get("estimated_vram_mb") or fallback_backend.get("estimated_vram_mb") or 0),
        "compose_file": str(backend.get("compose_file") or fallback_backend.get("compose_file") or "").strip(),
        "ready_path": str(backend.get("ready_path") or fallback_backend.get("ready_path") or "").strip(),
        "notes": str(backend.get("notes") or fallback_backend.get("notes") or "").strip(),
        "compose_managed": bool(backend.get("compose_managed", fallback_backend.get("compose_managed", True))),
    }


def integration_host_lanes() -> list[Dict[str, Any]]:
    lanes: list[Dict[str, Any]] = []
    for backend_name, label, route_kinds in (
        ("local_mlx", "ai2 / MLX", ["chat"]),
        ("local_vllm_fast", "ai1 / vLLM Fast", ["chat", "json"]),
        ("local_vllm_embeddings", "meltdown / vLLM Embeddings", ["embeddings"]),
        ("local_vllm", "ada2 / vLLM Strong", ["chat", "json"]),
        ("gpu_fast", "meltdown / SDXL-Turbo", ["images"]),
        ("gpu_heavy", "ada2 / InvokeAI Images", ["images"]),
    ):
        backend = _backend_profile(backend_name)
        if not backend.get("host"):
            continue
        summary_bits = [backend.get("host_description") or label]
        if backend.get("estimated_vram_mb"):
            summary_bits.append(f"~{backend['estimated_vram_mb']} MB comparable VRAM")
        lanes.append(
            {
                "id": backend_name,
                "label": label,
                "host": backend.get("host") or "",
                "runtime": "mlx" if backend_name == "local_mlx" else ("vllm" if backend_name.startswith("local_vllm") else "transformers"),
                "route_kinds": route_kinds,
                "summary": " | ".join(bit for bit in summary_bits if bit),
            }
        )
    return lanes


def _guess_parameter_billions(model_id: str, metadata: Dict[str, Any]) -> Optional[float]:
    tags = [str(item) for item in metadata.get("tags") or [] if str(item).strip()]
    text = " ".join([model_id, *tags])
    matches = []
    for match in re.finditer(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:b|bn)(?![a-z])", text, flags=re.IGNORECASE):
        try:
            value = float(match.group(1))
        except Exception:
            continue
        if 0.5 <= value <= 500:
            matches.append(value)
    if not matches:
        return None
    return max(matches)


def _deployment_target_from_backend(backend_name: str, *, reason: str, deployment_mode: Optional[str] = None) -> Dict[str, Any]:
    backend = _backend_profile(backend_name)
    target_mode = deployment_mode or ("host_native" if not backend.get("compose_managed", True) else "compose")
    return {
        "host": backend.get("host") or "",
        "host_description": backend.get("host_description") or "",
        "platform": backend.get("platform") or "",
        "resource_kind": backend.get("resource_kind") or "",
        "backend_lane": backend_name,
        "backend_display_name": backend.get("display_name") or backend_name,
        "deployment_mode": target_mode,
        "compose_file": "host-native" if target_mode == "host_native" else str(backend.get("compose_file") or ""),
        "estimated_vram_mb": int(backend.get("estimated_vram_mb") or 0),
        "ready_path": backend.get("ready_path") or "/readyz",
        "reason": reason,
        "notes": backend.get("notes") or "",
    }


def _recommend_deployment_target(runtime: str, route_kind: str, model_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    size_b = _guess_parameter_billions(model_id, metadata)
    if runtime == "mlx":
        return _deployment_target_from_backend(
            "local_mlx",
            reason="MLX models should stay on ai2 because that host is the Apple Silicon M3 Ultra lane with 512GB unified memory and a host-native MLX serving path.",
            deployment_mode="host_native",
        )
    if runtime == "vllm":
        if route_kind == "embeddings":
            return _deployment_target_from_backend(
                "local_vllm_embeddings",
                reason="Embeddings map to the dedicated meltdown embeddings lane, which uses the lighter 16GB CUDA host while leaving the larger chat lanes on ai1 and ada2.",
            )
        if size_b is not None and size_b >= 24:
            return _deployment_target_from_backend(
                "local_vllm",
                reason=f"The model looks roughly {size_b:g}B scale, which is better aligned with ada2's 48GB RTX 6000 Ada lane and 128GB system RAM for vLLM CPU offload headroom.",
            )
        return _deployment_target_from_backend(
            "local_vllm_fast",
            reason="Standard chat models that do not clearly need the heavy lane should start on ai1, which is the dual-NVIDIA fast vLLM path.",
        )
    if route_kind == "ocr":
        return _deployment_target_from_backend(
            "lighton_ocr",
            reason="OCR integrations line up with the existing CUDA OCR lane on ada2.",
        )
    if route_kind == "video":
        return _deployment_target_from_backend(
            "skyreels_v2",
            reason="Video generation belongs on ada2 because the tracked video lane already absorbs the highest CUDA and VRAM pressure in the cluster.",
        )
    if route_kind == "music":
        ada2 = _host_profile("ada2")
        return {
            "host": "ada2",
            "host_description": ada2.get("description") or "",
            "platform": ada2.get("platform") or "",
            "resource_kind": ada2.get("resource_kind") or "",
            "backend_lane": "new_music_backend",
            "backend_display_name": "New music backend",
            "deployment_mode": "compose",
            "compose_file": "docker-compose.<service>.yml",
            "estimated_vram_mb": 0,
            "ready_path": "/readyz",
            "reason": "No canonical production music backend is currently assigned; HeartMula was removed from the production plan. Pick a new runtime explicitly before scheduling it.",
            "notes": "",
        }
    if route_kind == "images":
        return _deployment_target_from_backend(
            "gpu_heavy",
            reason="Image-generation adapters should target the CUDA image lane on ada2 rather than the CPU or MLX hosts.",
        )
    if route_kind == "tts":
        ai2 = _host_profile("ai2")
        return {
            "host": "ai2",
            "host_description": ai2.get("description") or "",
            "platform": ai2.get("platform") or "",
            "resource_kind": ai2.get("resource_kind") or "",
            "backend_lane": "tts_ai2",
            "backend_display_name": "ai2 TTS lane",
            "deployment_mode": "compose",
            "compose_file": "docker-compose.<service>.yml",
            "estimated_vram_mb": 0,
            "ready_path": "/readyz",
            "reason": "TTS services in this cluster already live on ai2, where the M3 Ultra and large unified-memory pool are a better default fit than the CUDA image/video lanes.",
            "notes": "Promote to a CUDA host only if the selected model or runtime proves to require NVIDIA-specific acceleration.",
        }
    if size_b is not None and size_b >= 24:
        return _deployment_target_from_backend(
            "local_vllm",
            reason=f"Even with a transformers shim, a text model around {size_b:g}B scale should assume ada2-class GPU capacity first, not the lighter ai1 lane.",
        )
    return _deployment_target_from_backend(
        "local_vllm_fast",
        reason="Fallback text/json shims should start from ai1 unless the model clearly needs the heavier ada2 lane or an MLX host-native path.",
    )


def _is_existing_vllm_model_lane(runtime: str, route_kind: str) -> bool:
    return runtime == "vllm" and route_kind in {"chat", "embeddings"}


def _vllm_lane_env(plan: Dict[str, Any]) -> Dict[str, str]:
    backend = str((plan.get("deployment_target") or {}).get("backend_lane") or plan.get("backend_class") or "").strip()
    if backend == "local_vllm_embeddings":
        return {
            "model": "VLLM_MODEL_EMBEDDINGS",
            "served_model_name": "VLLM_EMBEDDINGS_SERVED_MODEL_NAME",
            "tokenizer": "VLLM_EMBEDDINGS_TOKENIZER",
            "compose_file": "docker-compose.vllm-embeddings.yml",
            "topology_model": "VLLM_MODEL_EMBEDDINGS",
        }
    if backend == "local_vllm":
        return {
            "model": "VLLM_MODEL_STRONG",
            "served_model_name": "VLLM_SERVED_MODEL_NAME",
            "tokenizer": "VLLM_TOKENIZER",
            "compose_file": "docker-compose.vllm-strong.yml",
            "topology_model": "VLLM_MODEL_STRONG",
        }
    return {
        "model": "VLLM_MODEL_FAST",
        "served_model_name": "VLLM_FAST_SERVED_MODEL_NAME",
        "tokenizer": "VLLM_FAST_TOKENIZER",
        "compose_file": "docker-compose.vllm-fast.yml",
        "topology_model": "VLLM_MODEL_FAST",
    }


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


def _runtime_from_metadata(model_id: str, metadata: Dict[str, Any], route_kind: str, *, preferred_runtime: Optional[str] = None) -> tuple[str, str]:
    preferred = str(preferred_runtime or "auto").strip().lower() or "auto"
    if preferred not in RUNTIME_CHOICES:
        raise ValueError("preferred_runtime must be one of: auto, mlx, vllm, transformers")
    if preferred != "auto":
        return preferred, f"Runtime pinned to `{preferred}` by the request."
    tags = {str(item).strip().lower() for item in metadata.get("tags") or [] if str(item).strip()}
    library_name = str(metadata.get("library_name") or "").strip().lower()
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    model_type = str(config.get("model_type") or "").strip().lower()
    pipeline_tag = str(metadata.get("pipeline_tag") or "").strip().lower()
    architectures = [str(item).strip().lower() for item in config.get("architectures") or [] if str(item).strip()]
    haystack = " ".join([model_id.lower(), library_name, model_type, pipeline_tag, *architectures, *sorted(tags)])
    if "mlx" in haystack:
        return "mlx", "Model metadata advertises an MLX-specific package or tag, so the host-native MLX path is the intended starting point."
    if route_kind not in {"chat", "embeddings"}:
        return "transformers", "Non-text modalities currently need the generic shim path instead of the vLLM text lanes."
    if any(marker in haystack for marker in _VLLM_UNSUPPORTED_MARKERS):
        return "transformers", "Model metadata points at a format or multimodal architecture that should not be treated as a plain vLLM text backend."
    if route_kind == "embeddings":
        return "vllm", "Text embeddings map cleanly to the existing vLLM embeddings lane on ai1."
    if pipeline_tag not in _TEXT_GENERATION_PIPELINES:
        return "transformers", f"Pipeline `{pipeline_tag}` is not a standard chat/text-generation lane for vLLM."
    if any("causallm" in architecture for architecture in architectures):
        return "vllm", "The architecture is a standard causal language model, which fits the vLLM chat lanes."
    if model_type in _VLLM_SUPPORTED_MODEL_TYPES:
        return "vllm", f"Model type `{model_type}` lines up with the supported vLLM text-serving lane."
    if library_name in {"", "transformers"} and not model_type:
        return "vllm", "Metadata looks like a standard transformers text model, so vLLM is the default runtime."
    return "transformers", "Metadata does not confidently match the supported vLLM text lane, so the safer default is a transformers shim."


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
    runtime, runtime_reason = _runtime_from_metadata(parsed["model_id"], metadata, selected_route, preferred_runtime=preferred_runtime)
    deployment_target = _recommend_deployment_target(runtime, selected_route, parsed["model_id"], metadata)
    existing_vllm_lane = _is_existing_vllm_model_lane(runtime, selected_route)
    normalized_service_name = _slugify(service_name or f"hf-{parsed['model_id'].replace('/', '-')}", default="hf-model-adapter")
    backend_class = (
        str(deployment_target.get("backend_lane") or "").strip()
        if existing_vllm_lane
        else normalized_service_name.replace("-", "_")
    )
    if not backend_class:
        backend_class = normalized_service_name.replace("-", "_")
    model_tail = parsed["model_id"].split("/", 1)[-1]
    display_name = f"HF {model_tail}"
    containerize = runtime != "mlx" and not existing_vllm_lane
    shim_required = runtime == "transformers"
    execution_mode = "existing_vllm_lane" if existing_vllm_lane else ("command" if shim_required else "upstream")
    upstream_base_url = "http://127.0.0.1:8000" if execution_mode == "upstream" else ""
    integration_strategy = "existing_vllm_model" if existing_vllm_lane else ("new_backend_service" if containerize else "host_native_runtime")
    if existing_vllm_lane:
        task_prompt = (
            f"Add the HuggingFace model {parsed['model_id']} to Nexus as an additional available model on the existing "
            f"{deployment_target.get('backend_display_name') or deployment_target.get('backend_lane') or 'vLLM'} lane "
            f"({backend_class}) for {selected_route}. Do not create a new backend class, service directory, registrar, "
            "or lifecycle backend for this plain vLLM text model unless repository evidence proves the existing lane cannot serve it. "
            f"Runtime strategy: {runtime}. Recommended deployment target: {deployment_target.get('host') or 'unknown host'} / "
            f"{deployment_target.get('backend_display_name') or deployment_target.get('backend_lane') or 'custom lane'}. "
            "Update the relevant vLLM env/topology/model-alias documentation or config with focused edits, preserve the existing repository README, "
            "and document any operational caveats such as tokenizer, served-model-name, context length, GPU memory, or gated-weight requirements."
        )
    else:
        task_prompt = (
            f"Integrate the HuggingFace model {parsed['model_id']} into Nexus as a {selected_route} backend. "
            f"Use the generated workspace scaffold. Runtime strategy: {runtime}. "
            f"Recommended deployment target: {deployment_target.get('host') or 'unknown host'} / {deployment_target.get('backend_display_name') or deployment_target.get('backend_lane') or 'custom lane'}. "
            f"Containerize the adapter if appropriate ({'yes' if containerize else 'no'}). "
            f"Provide an industry-standard API surface compatible with OpenAI-style {selected_route} access. "
            "Update env, compose or host-native launch files, backend registration snippets, implementation stubs, and focused documentation so the workspace is ready for Nexus integration. "
            "Preserve existing repository documentation; do not replace a root README wholesale."
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
        "runtime_reason": runtime_reason,
        "containerize": containerize,
        "shim_required": shim_required,
        "execution_mode": execution_mode,
        "integration_strategy": integration_strategy,
        "upstream_base_url": upstream_base_url,
        "service_name": normalized_service_name,
        "backend_class": backend_class,
        "target_backend_class": str(deployment_target.get("backend_lane") or backend_class),
        "display_name": display_name,
        "estimated_model_size_b": _guess_parameter_billions(parsed["model_id"], metadata),
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
        "deployment_target": deployment_target,
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


def _seed_path(repo_root: Path, preferred: Path, fallback_name: str) -> Path:
    if not preferred.exists():
        return preferred
    fallback = Path(fallback_name)
    stem = _slugify(fallback.stem, default="model-integration")
    return repo_root / "integration" / f"{stem}{fallback.suffix}"


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
    strategy = str(plan.get("integration_strategy") or "").strip()
    existing_vllm_lane = strategy == "existing_vllm_model"

    gitignore_path = repo_root / ".gitignore"
    if not gitignore_path.exists():
        _write(gitignore_path, "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n.venv/\nvenv/\ndist/\nbuild/\n*.pyc\n*.pyo\n*.pyd\noutputs/\n.runtime/\n")
        created.append(str(gitignore_path))

    warnings = "\n".join(f"- {item}" for item in plan.get("warnings") or []) or "- none"
    strategy_note = (
        "Existing vLLM lane model addition. This should update model availability/configuration for the existing backend lane, not add a new backend service."
        if existing_vllm_lane
        else "New backend or host-native runtime integration."
    )
    root_readme = (
        f"# {plan['display_name']} Nexus Integration Workspace\n\n"
        f"This coding workspace was generated for integrating the HuggingFace model `{plan['model_id']}` into Nexus.\n\n"
        "## Summary\n\n"
        f"- Source: {plan['source_url']}\n"
        f"- Route kind: `{plan['route_kind']}`\n"
        f"- Runtime strategy: `{plan['runtime']}`\n"
        f"- Runtime rationale: {plan.get('runtime_reason') or 'n/a'}\n"
        f"- Integration strategy: `{strategy or 'unspecified'}` - {strategy_note}\n"
        f"- Containerize: `{str(bool(plan['containerize'])).lower()}`\n"
        f"- Shim required: `{str(bool(plan['shim_required'])).lower()}`\n"
        f"- Service name: `{plan['service_name']}`\n"
        f"- Backend class: `{plan['backend_class']}`\n"
        f"- Target API path: `{plan['api_path']}`\n\n"
        "## Recommended Deployment Target\n\n"
        f"- Host: `{plan.get('deployment_target', {}).get('host') or 'unknown'}`\n"
        f"- Lane: `{plan.get('deployment_target', {}).get('backend_display_name') or plan.get('deployment_target', {}).get('backend_lane') or 'custom'}`\n"
        f"- Deployment mode: `{plan.get('deployment_target', {}).get('deployment_mode') or 'unknown'}`\n"
        f"- Comparable VRAM: `{plan.get('deployment_target', {}).get('estimated_vram_mb') or 0}` MB\n"
        f"- Reason: {plan.get('deployment_target', {}).get('reason') or 'n/a'}\n\n"
        "## Metadata Notes\n\n"
        f"- library: `{plan['hf_metadata'].get('library_name') or 'unknown'}`\n"
        f"- pipeline: `{plan['hf_metadata'].get('pipeline_tag') or 'unknown'}`\n"
        f"- gated: `{str(bool(plan['hf_metadata'].get('gated'))).lower()}`\n"
        f"- private: `{str(bool(plan['hf_metadata'].get('private'))).lower()}`\n\n"
        f"Warnings:\n{warnings}\n"
    )
    readme_path = _seed_path(repo_root, repo_root / "README.md", f"{plan['service_name']}-README.md")
    _write(readme_path, root_readme)
    created.append(str(readme_path))

    if existing_vllm_lane:
        lane_env = _vllm_lane_env(plan)
        agent_task = (
            "# Coding Agent Task\n\n"
            f"Goal:\n\n{plan['prompt']}\n\n"
            "Constraints:\n\n"
            f"1. Treat this as a model addition for existing backend lane `{plan['backend_class']}`, not a new backend integration.\n"
            "2. Do not create a new backend class, service directory, Dockerfile, service registrar, or lifecycle backend for this vLLM chat/embedding model unless repository evidence proves the existing lane cannot serve it.\n"
            "3. Preserve the repository root README and other broad docs. If documentation is needed, make a focused patch or use generated integration notes.\n"
            f"4. Inspect `{lane_env['compose_file']}`, `deploy/topology/production.json`, `.env.example`, and model alias configuration before editing.\n"
            f"5. Prefer updating model env/defaults such as `{lane_env['model']}`, `{lane_env['served_model_name']}`, and `{lane_env['tokenizer']}` plus aliases/catalog docs as appropriate.\n"
            "6. Run targeted validation and inspect the diff before finishing.\n"
        )
    else:
        agent_task = (
            "# Coding Agent Task\n\n"
            f"Goal:\n\n{plan['prompt']}\n\n"
            "Constraints:\n\n"
            "1. Reuse the generated scaffold instead of replacing it wholesale.\n"
            f"2. Keep the backend API compatible with `{plan['api_path']}`.\n"
            "3. Preserve existing repository files, especially the root README; use focused patches for existing docs.\n"
            "4. If runtime is `mlx`, keep the integration host-native and do not add Docker/Compose as the primary runtime path.\n"
            "5. If runtime is not `mlx`, provide a containerized path and keep the health/model metadata endpoints consistent with Nexus patterns.\n"
            "6. Update `integration/backend-config-snippet.yaml` and `integration/lifecycle.backend.json` so operators can wire the backend into Nexus.\n"
            "7. Document blockers for gated weights, unsupported architectures, or missing runtime features in focused integration docs.\n"
        )
    agent_task_path = _seed_path(repo_root, repo_root / "AGENT_TASK.md", f"{plan['service_name']}-AGENT_TASK.md")
    _write(agent_task_path, agent_task)
    created.append(str(agent_task_path))

    request_path = _seed_path(repo_root, repo_root / "integration_request.json", f"{plan['service_name']}-integration_request.json")
    _write(request_path, json.dumps(plan, indent=2, sort_keys=True) + "\n")
    created.append(str(request_path))

    if existing_vllm_lane:
        lane_env = _vllm_lane_env(plan)
        model_id = str(plan["model_id"])
        alias_name = _slugify(model_id.split("/", 1)[-1], default="hf-model")
        env_snippet = (
            f"# Add `{model_id}` to the existing Nexus vLLM lane `{plan['backend_class']}`.\n"
            "# Apply these values in the host-specific env/topology layer for the target host; do not create a new backend service.\n"
            f"{lane_env['model']}={model_id}\n"
            f"{lane_env['served_model_name']}={model_id}\n"
            f"{lane_env['tokenizer']}={model_id}\n"
        )
        _write(repo_root / "integration" / "vllm-model-env-snippet.env", env_snippet)
        created.append(str(repo_root / "integration" / "vllm-model-env-snippet.env"))

        alias_snippet = {
            "aliases": {
                alias_name: {
                    "backend": plan["backend_class"],
                    "model": model_id,
                    "tools": False,
                }
            }
        }
        _write(repo_root / "integration" / "model-alias-snippet.json", json.dumps(alias_snippet, indent=2, sort_keys=True) + "\n")
        created.append(str(repo_root / "integration" / "model-alias-snippet.json"))

        checklist = (
            "# Existing vLLM Lane Checklist\n\n"
            f"- Target backend lane: `{plan['backend_class']}`\n"
            f"- Target host: `{plan.get('deployment_target', {}).get('host') or 'unknown'}`\n"
            f"- Compose file: `{lane_env['compose_file']}`\n"
            f"- Model env key: `{lane_env['model']}`\n"
            f"- Served-model-name key: `{lane_env['served_model_name']}`\n"
            f"- Tokenizer key: `{lane_env['tokenizer']}`\n\n"
            "Expected implementation shape:\n\n"
            "1. Keep the existing backend class and lifecycle entry.\n"
            "2. Add or document the model as an available served model for that lane.\n"
            "3. Add a model alias only if Nexus should expose a stable user-facing model name.\n"
            "4. Update deployment/topology docs or env examples with focused patches.\n"
            "5. Avoid new `services/<model>` scaffolding for plain vLLM text models.\n"
        )
        _write(repo_root / "integration" / "vllm-lane-checklist.md", checklist)
        created.append(str(repo_root / "integration" / "vllm-lane-checklist.md"))
        return created

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
            "host": str(plan.get("deployment_target", {}).get("host") or "set-me"),
            "component": plan["service_name"],
            "tier": "optional",
            "capabilities": [plan["route_kind"]],
            "estimated_vram_mb": int(plan.get("deployment_target", {}).get("estimated_vram_mb") or 0),
            "auto_start": True,
            "auto_stop": True,
            "compose_file": f"docker-compose.{plan['service_name']}.yml" if bool(plan["containerize"]) else "host-native",
            "ready_path": "/readyz" if bool(plan["shim_required"]) or bool(plan["containerize"]) else "/v1/models",
            "notes": (
                f"Generated from HuggingFace model {plan['model_id']}. "
                f"Recommended lane: {plan.get('deployment_target', {}).get('host') or 'unknown'} / "
                f"{plan.get('deployment_target', {}).get('backend_display_name') or plan.get('deployment_target', {}).get('backend_lane') or 'custom lane'}. "
                f"{plan.get('deployment_target', {}).get('reason') or 'Fill in host, VRAM, secrets, and artifact requirements.'}"
            ),
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
