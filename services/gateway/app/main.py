"""
Nexus Gateway - Container-based AI orchestration gateway

This is a minimal gateway implementation that demonstrates the standardized API pattern.
For the full-featured gateway, see the gevanoff/gateway repository.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import asyncio
import base64
import httpx
import json
import os
import logging
import re
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Invalid integer for %s: %s (using %s)", name, value, default)
        return default
    return parsed


def _get_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logger.warning("Invalid float for %s: %s (using %s)", name, value, default)
        return default
    return parsed


# Configuration from environment
GATEWAY_BEARER_TOKEN = os.getenv("GATEWAY_BEARER_TOKEN", "change-me")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "ollama")
MAX_REQUEST_BYTES = _get_int_env("MAX_REQUEST_BYTES", 10485760)

ETCD_ENABLED = os.getenv("ETCD_ENABLED", "false").lower() in {"1", "true", "yes"}
ETCD_URL = os.getenv("ETCD_URL", "http://etcd:2379")
ETCD_PREFIX = os.getenv("ETCD_PREFIX", "/nexus/services/")
ETCD_POLL_INTERVAL = _get_float_env("ETCD_POLL_INTERVAL", 15.0)
ETCD_SEED_FROM_ENV = os.getenv("ETCD_SEED_FROM_ENV", "true").lower() in {"1", "true", "yes"}
GATEWAY_SERVICE_URL = os.getenv("GATEWAY_SERVICE_URL")
HTTPX_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
HTTPX_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20)

# Initialize FastAPI app
app = FastAPI(
    title="Nexus Gateway",
    version="1.0.0",
    description="Container-based AI orchestration gateway"
)


# ===== Service Discovery (etcd) =====

class ServiceRecord(BaseModel):
    name: str
    base_url: str
    metadata_url: Optional[str] = None

    def normalized(self) -> "ServiceRecord":
        if not re.fullmatch(r"[a-z0-9-]+", self.name):
            raise ValueError(f"Invalid service name: {self.name}")
        base_url = self.base_url.rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported scheme for service URL: {base_url}")
        metadata_url = self.metadata_url.rstrip("/") if self.metadata_url else f"{base_url}/v1/metadata"
        return ServiceRecord(name=self.name, base_url=base_url, metadata_url=metadata_url)


class EtcdClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _b64(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("utf-8")

    @staticmethod
    def _prefix_range_end(prefix: str) -> str:
        if not prefix:
            return "\0"
        prefix_bytes = bytearray(prefix.encode("utf-8"))
        for i in range(len(prefix_bytes) - 1, -1, -1):
            if prefix_bytes[i] < 0xFF:
                prefix_bytes[i] += 1
                return prefix_bytes[: i + 1].decode("utf-8")
        return "\0"

    async def put(self, key: str, value: Dict[str, Any]) -> None:
        payload = {
            "key": self._b64(key),
            "value": self._b64(json.dumps(value))
        }
        async with httpx.AsyncClient(timeout=self.timeout, limits=HTTPX_LIMITS) as client:
            response = await client.post(f"{self.base_url}/v3/kv/put", json=payload)
            response.raise_for_status()

    async def get_prefix(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        range_end = self._prefix_range_end(prefix)
        payload = {
            "key": self._b64(prefix),
            "range_end": self._b64(range_end)
        }
        async with httpx.AsyncClient(timeout=self.timeout, limits=HTTPX_LIMITS) as client:
            response = await client.post(f"{self.base_url}/v3/kv/range", json=payload)
            response.raise_for_status()
            data = response.json()
        kvs = data.get("kvs", [])
        decoded: Dict[str, Dict[str, Any]] = {}
        for kv in kvs:
            key = base64.b64decode(kv["key"]).decode("utf-8")
            value = base64.b64decode(kv["value"]).decode("utf-8")
            decoded[key] = json.loads(value)
        return decoded


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: Dict[str, ServiceRecord] = {}
        self._metadata_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._etcd = EtcdClient(ETCD_URL) if ETCD_ENABLED else None

    async def seed_from_env(self) -> None:
        static_services = []
        if OLLAMA_BASE_URL:
            static_services.append(ServiceRecord(name="ollama", base_url=OLLAMA_BASE_URL))
        images_url = os.getenv("IMAGES_HTTP_BASE_URL")
        if images_url:
            static_services.append(ServiceRecord(name="images", base_url=images_url))
        audio_url = os.getenv("AUDIO_BACKEND_URL")
        if audio_url:
            static_services.append(ServiceRecord(name="tts", base_url=audio_url))

        async with self._lock:
            for service in static_services:
                try:
                    normalized = service.normalized()
                except ValueError as exc:
                    logger.warning("Skipping invalid service config: %s", exc)
                    continue
                self._services[normalized.name] = normalized

        if self._etcd and ETCD_SEED_FROM_ENV:
            for service in static_services:
                try:
                    record = service.normalized()
                except ValueError as exc:
                    logger.warning("Skipping invalid service config: %s", exc)
                    continue
                await self._etcd.put(f"{ETCD_PREFIX}{record.name}", record.model_dump())

        if self._etcd and GATEWAY_SERVICE_URL:
            try:
                gateway_record = ServiceRecord(name="gateway", base_url=GATEWAY_SERVICE_URL).normalized()
                await self._etcd.put(f"{ETCD_PREFIX}gateway", gateway_record.model_dump())
            except ValueError as exc:
                logger.warning("Skipping invalid gateway service URL: %s", exc)

    async def refresh_from_etcd(self) -> None:
        if not self._etcd:
            return
        data = await self._etcd.get_prefix(ETCD_PREFIX)
        updated: Dict[str, ServiceRecord] = {}
        for _, payload in data.items():
            try:
                record = ServiceRecord(**payload).normalized()
                updated[record.name] = record
            except Exception as exc:
                logger.warning("Skipping invalid service record: %s", exc)
        if updated:
            async with self._lock:
                self._services.update(updated)

    async def get_service(self, name: str) -> Optional[ServiceRecord]:
        async with self._lock:
            return self._services.get(name)

    async def list_services(self) -> List[ServiceRecord]:
        async with self._lock:
            return list(self._services.values())

    async def set_service_metadata(self, name: str, metadata: Dict[str, Any]) -> None:
        async with self._lock:
            self._metadata_cache[name] = metadata

    async def get_service_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return self._metadata_cache.get(name)


async def fetch_service_descriptor(service: ServiceRecord) -> Dict[str, Any]:
    """Fetch enhanced service descriptor, falling back to /v1/metadata."""
    descriptor_url = f"{service.base_url}/v1/descriptor"
    metadata_url = service.metadata_url or f"{service.base_url}/v1/metadata"
    async with httpx.AsyncClient(timeout=10.0, limits=HTTPX_LIMITS) as client:
        descriptor_response = await client.get(descriptor_url)
        if descriptor_response.status_code == 200:
            data = descriptor_response.json()
            if isinstance(data, dict):
                return data

        metadata_response = await client.get(metadata_url)
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if not isinstance(metadata, dict):
            raise ValueError(f"Invalid metadata payload for service {service.name}")
        return {
            "schema_version": metadata.get("schema_version", "v1"),
            "service": metadata.get("service", {"name": service.name}),
            "capabilities": metadata.get("capabilities", {}),
            "endpoints": metadata.get("endpoints", []),
            "response_types": {
                "default": "application/json",
                "streaming": "text/event-stream" if metadata.get("capabilities", {}).get("streaming") else None,
            },
            "ui": metadata.get("ui", {}),
            "ui_navigation": {
                "placement": "side-panel",
                "group": "tools"
            }
        }


def build_ui_layout(descriptors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Create a dynamic UI layout with Chat as primary and backend-specific panels."""
    primary = {
        "id": "chat",
        "label": "Chat",
        "type": "primary",
        "endpoint": "/v1/chat/completions"
    }
    panels = []
    for descriptor in descriptors:
        service = descriptor.get("service", {})
        service_name = service.get("name", "unknown")
        capabilities = descriptor.get("capabilities", {})
        domains = capabilities.get("domains", [])
        ui_navigation = descriptor.get("ui_navigation", {})
        panels.append({
            "id": service_name,
            "label": service.get("name", service_name).title(),
            "placement": ui_navigation.get("placement", "side-panel"),
            "group": ui_navigation.get("group", "tools"),
            "domains": domains,
            "ui_options": descriptor.get("ui", {}).get("options", []),
            "endpoints": descriptor.get("endpoints", [])
        })

    return {
        "primary": primary,
        "panels": panels,
        "notes": "Gateway should render Chat first, and backend panels as specialized tool tabs."
    }


service_registry = ServiceRegistry()


@app.on_event("startup")
async def startup_event() -> None:
    await service_registry.seed_from_env()

    async def refresh_descriptors() -> None:
        services = await service_registry.list_services()
        for service in services:
            try:
                descriptor = await fetch_service_descriptor(service)
                await service_registry.set_service_metadata(service.name, descriptor)
            except Exception as exc:
                logger.warning("Failed to refresh descriptor for %s: %s", service.name, exc)

    await refresh_descriptors()

    if ETCD_ENABLED:
        async def poll_etcd() -> None:
            while True:
                try:
                    await service_registry.refresh_from_etcd()
                    await refresh_descriptors()
                except Exception as exc:
                    logger.warning("Failed to refresh etcd services: %s", exc)
                await asyncio.sleep(ETCD_POLL_INTERVAL)

        asyncio.create_task(poll_etcd())


# ===== Request Size Guard =====

@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "Invalid Content-Length"})
        if length > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"error": "Request too large"})

    if request.method in {"POST", "PUT", "PATCH"} and content_length is None:
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"error": "Request too large"})
        request._body = body

    return await call_next(request)

# ===== Authentication =====

def verify_bearer_token(authorization: Optional[str] = Header(None)) -> bool:
    """Verify bearer token authentication"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = authorization[7:]  # Remove "Bearer " prefix
    
    if token != GATEWAY_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    return True


# ===== Health and Metadata Endpoints =====

@app.get("/health")
async def health():
    """Liveness check - is the service running?"""
    return {"status": "ok"}


@app.get("/readyz")
async def readiness():
    """Readiness check - can the service handle requests?"""
    checks = {}
    
    # Check Ollama backend
    try:
        ollama_service = await service_registry.get_service("ollama")
        ollama_url = ollama_service.base_url if ollama_service else OLLAMA_BASE_URL
        async with httpx.AsyncClient(timeout=5.0, limits=HTTPX_LIMITS) as client:
            response = await client.get(f"{ollama_url}/api/tags")
            checks["ollama"] = "ok" if response.status_code == 200 else "error"
    except Exception as e:
        checks["ollama"] = f"error: {str(e)}"
    
    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503
    
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks}
    )


@app.get("/v1/metadata")
async def metadata():
    """Service discovery - advertise capabilities and endpoints"""
    return {
        "schema_version": "v1",
        "service": {
            "name": "gateway",
            "version": "1.0.0",
            "description": "Nexus Gateway - AI orchestration API gateway"
        },
        "endpoints": [
            {
                "path": "/health",
                "method": "GET",
                "operation_id": "health.check",
                "summary": "Liveness check"
            },
            {
                "path": "/readyz",
                "method": "GET",
                "operation_id": "readiness.check",
                "summary": "Readiness check"
            },
            {
                "path": "/v1/metadata",
                "method": "GET",
                "operation_id": "metadata.get",
                "summary": "Service discovery"
            },
            {
                "path": "/v1/descriptor",
                "method": "GET",
                "operation_id": "descriptor.get",
                "summary": "Enhanced service descriptor"
            },
            {
                "path": "/v1/chat/completions",
                "method": "POST",
                "operation_id": "chat.completions.create",
                "summary": "Create chat completion"
            },
            {
                "path": "/v1/models",
                "method": "GET",
                "operation_id": "models.list",
                "summary": "List available models"
            },
            {
                "path": "/v1/registry",
                "method": "GET",
                "operation_id": "registry.list",
                "summary": "List registered services"
            },
            {
                "path": "/v1/backends/catalog",
                "method": "GET",
                "operation_id": "backends.catalog",
                "summary": "List backend descriptors and UI metadata"
            },
            {
                "path": "/v1/ui/layout",
                "method": "GET",
                "operation_id": "ui.layout",
                "summary": "Render layout hints for dynamic specialized backend UI"
            }
        ],
        "capabilities": {
            "domains": ["chat", "completion", "embedding"],
            "modalities": ["text"],
            "streaming": True,
            "max_concurrency": 10
        },
        "backends": {
            "default": DEFAULT_BACKEND,
            "available": ["ollama"]
        }
    }


@app.get("/v1/descriptor")
async def descriptor():
    """Enhanced capability descriptor including response types and UI placement hints."""
    base = await metadata()
    base["response_types"] = {
        "default": "application/json",
        "streaming": "text/event-stream"
    }
    base["ui_navigation"] = {
        "placement": "primary",
        "group": "chat"
    }
    base["ui"] = {
        "options": [
            {
                "key": "model",
                "label": "Model",
                "type": "enum",
                "description": "Model used for chat inference"
            },
            {
                "key": "temperature",
                "label": "Temperature",
                "type": "number",
                "default": 0.7,
                "min": 0.0,
                "max": 2.0,
                "description": "Sampling temperature"
            }
        ]
    }
    return base


# ===== Models Endpoint =====

@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(None)):
    """List available models from backend services"""
    verify_bearer_token(authorization)
    
    try:
        backend = await service_registry.get_service(DEFAULT_BACKEND)
        base_url = backend.base_url if backend else OLLAMA_BASE_URL
        async with httpx.AsyncClient(timeout=10.0, limits=HTTPX_LIMITS) as client:
            # Get models from Ollama
            response = await client.get(f"{base_url}/api/tags")
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Backend unavailable")
            
            data = response.json()
            models = data.get("models", [])
            
            # Convert to OpenAI format
            model_list = [
                {
                    "id": model.get("name", "unknown"),
                    "object": "model",
                    "created": 0,
                    "owned_by": "ollama"
                }
                for model in models
            ]
            
            return {
                "object": "list",
                "data": model_list
            }
    
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        raise HTTPException(status_code=502, detail=f"Error listing models: {str(e)}")


@app.get("/v1/registry")
async def list_registry(authorization: Optional[str] = Header(None)):
    """List registered service records"""
    verify_bearer_token(authorization)
    services = await service_registry.list_services()
    return {
        "object": "list",
        "data": [service.model_dump() for service in services]
    }


@app.get("/v1/backends/catalog")
async def backend_catalog(authorization: Optional[str] = Header(None)):
    """Return full backend descriptors for dynamic UI generation."""
    verify_bearer_token(authorization)
    services = await service_registry.list_services()
    catalog = []
    for service in services:
        descriptor = await service_registry.get_service_metadata(service.name)
        if descriptor is None:
            try:
                descriptor = await fetch_service_descriptor(service)
                await service_registry.set_service_metadata(service.name, descriptor)
            except Exception as exc:
                logger.warning("Descriptor fetch failed for %s: %s", service.name, exc)
                descriptor = {
                    "service": {"name": service.name},
                    "capabilities": {},
                    "endpoints": [],
                    "response_types": {"default": "application/json"},
                    "ui": {"options": []}
                }
        catalog.append({
            "service_record": service.model_dump(),
            "descriptor": descriptor
        })

    return {
        "object": "list",
        "data": catalog
    }


@app.get("/v1/ui/layout")
async def ui_layout(authorization: Optional[str] = Header(None)):
    """Return UI organization model for Chat + specialized backend tabs."""
    verify_bearer_token(authorization)
    services = await service_registry.list_services()
    descriptors = []
    for service in services:
        descriptor = await service_registry.get_service_metadata(service.name)
        if descriptor:
            descriptors.append(descriptor)
    return build_ui_layout(descriptors)


# ===== Chat Completions Endpoint =====

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None)
):
    """Create a chat completion"""
    verify_bearer_token(authorization)
    
    try:
        backend = await service_registry.get_service(DEFAULT_BACKEND)
        base_url = backend.base_url if backend else OLLAMA_BASE_URL
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT, limits=HTTPX_LIMITS) as client:
            # Convert to Ollama format
            ollama_request = {
                "model": request.model,
                "messages": [
                    {"role": msg.role, "content": msg.content}
                    for msg in request.messages
                ],
                "stream": request.stream,
                "options": {
                    "temperature": request.temperature
                }
            }
            
            if request.max_tokens:
                ollama_request["options"]["num_predict"] = request.max_tokens
            
            # Forward to Ollama
            if request.stream:
                # Streaming response
                async def stream_response():
                    async with client.stream(
                        "POST",
                        f"{base_url}/api/chat",
                        json=ollama_request
                    ) as response:
                        if response.status_code != 200:
                            detail = await response.aread()
                            raise HTTPException(status_code=502, detail=detail.decode("utf-8", "ignore"))
                        async for line in response.aiter_lines():
                            if line.strip():
                                yield f"data: {line}\n\n"
                        yield "data: [DONE]\n\n"
                
                return StreamingResponse(
                    stream_response(),
                    media_type="text/event-stream"
                )
            else:
                # Non-streaming response
                response = await client.post(
                    f"{base_url}/api/chat",
                    json=ollama_request
                )
                
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail="Backend error"
                    )
                
                data = response.json()
                
                # Convert to OpenAI format
                return {
                    "id": "chatcmpl-" + os.urandom(12).hex(),
                    "object": "chat.completion",
                    "created": 0,
                    "model": request.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": data.get("message", {}).get("content", "")
                            },
                            "finish_reason": "stop"
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    }
                }
    
    except Exception as e:
        logger.error(f"Error in chat completion: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Error processing request: {str(e)}"
        )


# ===== Root endpoint =====

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Nexus Gateway",
        "version": "1.0.0",
        "endpoints": [
            "/health",
            "/readyz",
            "/v1/metadata",
            "/v1/descriptor",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/registry",
            "/v1/backends/catalog",
            "/v1/ui/layout"
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8800)
