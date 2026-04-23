"""Backend configuration and admission control.

Single source of truth for backend classes, capabilities, concurrency limits,
and payload policies. Provides deterministic routing and fast-fail overload protection.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from dataclasses import replace
import json
import os
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastapi import HTTPException

from app.config import S, logger
from app.httpx_client import httpx_client as _httpx_client


RouteKind = Literal["chat", "embeddings", "images", "music", "tts", "video", "ocr", "transcription"]


@dataclass(frozen=True)
class BackendConfig:
    """Configuration for a single backend class."""

    backend_class: str
    provider: str
    base_url: str
    description: str
    supported_capabilities: List[RouteKind]
    concurrency_limits: Dict[RouteKind, int]
    health_liveness: str
    health_readiness: str
    payload_policy: Dict[str, Any]

    def supports(self, route_kind: RouteKind) -> bool:
        """Check if this backend supports the given route kind."""
        return route_kind in self.supported_capabilities

    def get_limit(self, route_kind: RouteKind) -> int:
        """Get concurrency limit for a route kind."""
        return self.concurrency_limits.get(route_kind, 1)


@dataclass(frozen=True)
class ServiceRecord:
    """Service registry record discovered from etcd or synthesized from env."""

    name: str
    base_url: str
    metadata_url: str = ""
    backend_class: str = ""
    hostname: str = ""
    source: str = "unknown"


@dataclass
class BackendRegistry:
    """Registry of all backend configurations."""

    backends: Dict[str, BackendConfig]
    legacy_mapping: Dict[str, str]
    static_backends: Dict[str, BackendConfig] = field(default_factory=dict)
    service_records: Dict[str, ServiceRecord] = field(default_factory=dict)

    def get_backend(self, backend_class: str) -> Optional[BackendConfig]:
        """Get backend config by class name."""
        actual_class = self.resolve_backend_class(backend_class)
        return self.backends.get(actual_class)

    def resolve_backend_class(self, backend_name: str) -> str:
        """Resolve a backend name (including legacy names) to its canonical class."""
        raw = (backend_name or "").strip()
        if not raw:
            return raw
        if raw in self.backends:
            return raw
        lowered = raw.lower()
        if lowered in self.backends:
            return lowered
        mapped = self.legacy_mapping.get(raw, self.legacy_mapping.get(lowered, raw))
        if mapped in self.backends:
            return mapped
        normalized = lowered.replace("_", "-")
        for candidate in self.backends.keys():
            if candidate.lower().replace("_", "-") == normalized:
                return candidate
        if mapped != raw:
            return mapped
        return raw


def _backend_host(base_url: str) -> Optional[str]:
    try:
        parsed = urlparse(base_url or "")
    except Exception:
        return None
    host = parsed.hostname or parsed.netloc
    if not host:
        return None
    return host


_SERVICE_NAME_TO_BACKEND_CLASS: Dict[str, str] = {
    "ollama": "local_vllm",
    "vllm": "local_vllm",
    "local_vllm": "local_vllm",
    "vllm-fast": "local_vllm_fast",
    "vllm_fast": "local_vllm_fast",
    "local_vllm_fast": "local_vllm_fast",
    "vllm-embeddings": "local_vllm_embeddings",
    "vllm_embeddings": "local_vllm_embeddings",
    "local_vllm_embeddings": "local_vllm_embeddings",
    "mlx": "local_mlx",
    "local_mlx": "local_mlx",
    "invokeai": "gpu_heavy",
    "images": "gpu_heavy",
    "gpu_heavy": "gpu_heavy",
    "sdxl-turbo": "gpu_fast",
    "sdxl_turbo": "gpu_fast",
    "gpu_fast": "gpu_fast",
    "heartmula": "heartmula_music",
    "heartmula_music": "heartmula_music",
    "tts": "pocket_tts",
    "pocket-tts": "pocket_tts",
    "pocket_tts": "pocket_tts",
    "qwen3-tts": "qwen3_tts",
    "qwen3_tts": "qwen3_tts",
    "luxtts": "luxtts",
    "lighton-ocr": "lighton_ocr",
    "lighton_ocr": "lighton_ocr",
    "personaplex": "personaplex",
    "followyourcanvas": "followyourcanvas",
    "skyreels-v2": "skyreels_v2",
    "skyreels_v2": "skyreels_v2",
}

_BACKEND_CLASS_TO_SERVICE_NAME: Dict[str, str] = {
    "local_vllm": "vllm",
    "local_vllm_fast": "vllm-fast",
    "local_vllm_embeddings": "vllm-embeddings",
    "local_mlx": "mlx",
    "gpu_heavy": "images",
    "gpu_fast": "sdxl-turbo",
    "heartmula_music": "heartmula",
    "pocket_tts": "tts",
    "qwen3_tts": "qwen3-tts",
    "luxtts": "luxtts",
    "lighton_ocr": "lighton-ocr",
    "personaplex": "personaplex",
    "followyourcanvas": "followyourcanvas",
    "skyreels_v2": "skyreels-v2",
}


def _service_name_to_backend_class(service_name: str, registry: BackendRegistry) -> Optional[str]:
    normalized = (service_name or "").strip().lower()
    if not normalized:
        return None
    mapped = _SERVICE_NAME_TO_BACKEND_CLASS.get(normalized)
    if mapped:
        return mapped
    for prefix, mapped in _SERVICE_NAME_TO_BACKEND_CLASS.items():
        if normalized.startswith(prefix + "-") or normalized.startswith(prefix + "_"):
            return mapped
    normalized = normalized.replace("-", "_")
    if normalized in registry.static_backends:
        return normalized
    return None


def _normalize_backend_class(backend_class: str, registry: BackendRegistry) -> Optional[str]:
    normalized = (backend_class or "").strip().lower()
    if not normalized:
        return None
    normalized = normalized.replace("-", "_")
    resolved = registry.resolve_backend_class(normalized)
    if resolved in registry.static_backends:
        return resolved
    if normalized in registry.static_backends:
        return normalized
    return None


def _backend_class_to_service_name(backend_class: str) -> str:
    return _BACKEND_CLASS_TO_SERVICE_NAME.get(backend_class, backend_class)


def _normalize_backend_name(name: str) -> str:
    return (name or "").strip().lower()


def backend_provider_name(backend_name: str) -> str:
    registry = get_registry()
    resolved = registry.resolve_backend_class(backend_name)
    cfg = registry.get_backend(resolved)
    provider = (cfg.provider if cfg else "").strip().lower()
    if provider:
        return provider

    normalized = _normalize_backend_name(resolved or backend_name).replace("_", "-")
    if normalized in {"vllm", "local-vllm"} or normalized.startswith("vllm-") or normalized.endswith("-vllm"):
        return "vllm"
    if normalized in {"mlx", "local-mlx"} or normalized.startswith("mlx-") or normalized.endswith("-mlx"):
        return "mlx"
    return normalized


def llm_backends() -> list[tuple[str, BackendConfig]]:
    registry = get_registry()
    out: list[tuple[str, BackendConfig]] = []
    for backend_name, cfg in registry.backends.items():
        if "chat" not in cfg.supported_capabilities:
            continue
        if not (cfg.base_url or "").strip():
            continue
        if backend_provider_name(backend_name) not in {"vllm", "mlx"}:
            continue
        out.append((backend_name, cfg))
    out.sort(key=lambda item: item[0])
    return out


def get_service_record_for_backend(
    backend_name: str,
    *,
    registry: Optional[BackendRegistry] = None,
) -> Optional[ServiceRecord]:
    reg = registry or get_registry()
    resolved = reg.resolve_backend_class(backend_name)
    for candidate in (backend_name, resolved):
        if not candidate:
            continue
        record = reg.service_records.get(candidate)
        if record is not None:
            return record
    for record in reg.service_records.values():
        if record.backend_class in {backend_name, resolved}:
            return record
    return None


def backend_hostname(
    backend_name: str,
    *,
    registry: Optional[BackendRegistry] = None,
    fallback_base_url: str = "",
) -> str:
    record = get_service_record_for_backend(backend_name, registry=registry)
    hostname = (record.hostname if record is not None else "").strip()
    if hostname:
        return hostname
    base_url = (fallback_base_url or (record.base_url if record is not None else "")).strip()
    return _backend_host(base_url) or ""


def _prefix_range_end(prefix: str) -> str:
    if not prefix:
        return "\0"
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _decode_b64(raw: str) -> str:
    return base64.b64decode(raw.encode("ascii")).decode("utf-8")


async def _fetch_etcd_service_records() -> Dict[str, ServiceRecord]:
    prefix = (getattr(S, "ETCD_PREFIX", "") or "").strip() or "/nexus/services/"
    payload = {
        "key": base64.b64encode(prefix.encode("utf-8")).decode("ascii"),
        "range_end": base64.b64encode(_prefix_range_end(prefix).encode("utf-8")).decode("ascii"),
    }
    records: Dict[str, ServiceRecord] = {}
    async with _httpx_client(timeout=getattr(S, "ETCD_TIMEOUT_SEC", 5.0) or 5.0) as client:
        response = await client.post(f"{S.ETCD_URL.rstrip('/')}/v3/kv/range", json=payload)
        response.raise_for_status()
        data = response.json()
    for item in data.get("kvs", []) if isinstance(data, dict) else []:
        try:
            key = _decode_b64(str(item.get("key", "")))
            raw_value = _decode_b64(str(item.get("value", "")))
            value = json.loads(raw_value)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or "").strip()
        if not name:
            name = key.rsplit("/", 1)[-1].strip()
        base_url = _sanitize_base_url(str(value.get("base_url") or ""))
        metadata_url = _sanitize_base_url(str(value.get("metadata_url") or ""))
        if not name or not base_url:
            continue
        records[name] = ServiceRecord(
            name=name,
            base_url=base_url,
            metadata_url=metadata_url,
            backend_class=str(value.get("backend_class") or "").strip(),
            hostname=str(value.get("hostname") or "").strip(),
            source="etcd",
        )
    return records


def _build_seed_records(registry: BackendRegistry) -> Dict[str, ServiceRecord]:
    if not getattr(S, "ETCD_SEED_FROM_ENV", True):
        return {}
    seeded: Dict[str, ServiceRecord] = {}
    for backend_class, config in registry.static_backends.items():
        base_url = _sanitize_base_url(config.base_url)
        if not base_url:
            continue
        service_name = _backend_class_to_service_name(backend_class)
        seeded[service_name] = ServiceRecord(
            name=service_name,
            base_url=base_url,
            metadata_url=f"{base_url.rstrip('/')}/v1/metadata",
            backend_class=backend_class,
            hostname="",
            source="env",
        )
    return seeded


def _apply_service_records(registry: BackendRegistry, service_records: Dict[str, ServiceRecord]) -> None:
    effective = dict(registry.static_backends)
    bound_records: Dict[str, ServiceRecord] = {}
    for record in service_records.values():
        explicit_backend_class = _normalize_backend_class(record.backend_class, registry)
        backend_class = explicit_backend_class
        if not backend_class:
            backend_class = _service_name_to_backend_class(record.name, registry)
        if not backend_class:
            continue
        config = registry.static_backends.get(backend_class)
        if config is None:
            continue
        record_name = _normalize_backend_name(record.name)
        canonical_service_name = _normalize_backend_name(_backend_class_to_service_name(backend_class))
        if explicit_backend_class is None and record_name not in {"", canonical_service_name, _normalize_backend_name(backend_class)}:
            logger.info(
                "Skipping non-canonical service record %s for backend %s because it did not declare an explicit backend_class",
                record.name,
                backend_class,
            )
            continue
        backend_key = backend_class if record_name in {"", canonical_service_name, _normalize_backend_name(backend_class)} else record_name
        description = config.description if backend_key == backend_class else f"{config.description} ({record.name})"
        effective[backend_key] = replace(
            config,
            backend_class=backend_key,
            base_url=record.base_url,
            description=description,
        )
        bound_records[record.name] = replace(record, backend_class=backend_key)
    registry.backends = effective
    registry.service_records = bound_records


async def refresh_registry_from_etcd() -> None:
    registry = get_registry()
    service_records = _build_seed_records(registry)
    if getattr(S, "ETCD_ENABLED", True):
        try:
            fetched = await _fetch_etcd_service_records()
            service_records.update(fetched)
        except Exception as exc:
            logger.warning("etcd registry refresh failed: %s: %s", type(exc).__name__, exc)
    _apply_service_records(registry, service_records)
    if _admission is not None:
        _admission.sync_registry(registry)


_registry_sync_task: Optional[asyncio.Task] = None


async def _registry_sync_loop() -> None:
    interval = float(getattr(S, "ETCD_POLL_INTERVAL", 15.0) or 15.0)
    while True:
        await asyncio.sleep(max(interval, 1.0))
        await refresh_registry_from_etcd()


async def start_registry_sync() -> None:
    global _registry_sync_task
    await refresh_registry_from_etcd()
    if not getattr(S, "ETCD_ENABLED", True):
        return
    if _registry_sync_task is not None and not _registry_sync_task.done():
        return
    _registry_sync_task = asyncio.create_task(_registry_sync_loop())
    logger.info("Backend registry sync started")


async def stop_registry_sync() -> None:
    global _registry_sync_task
    if _registry_sync_task is None:
        return
    _registry_sync_task.cancel()
    try:
        await _registry_sync_task
    except asyncio.CancelledError:
        pass
    _registry_sync_task = None
    logger.info("Backend registry sync stopped")


def _sanitize_base_url(raw_base_url: str) -> str:
    candidate = raw_base_url.strip()
    if not candidate:
        return ""
    if any(ch in candidate for ch in ("\n", "\r", "\t")):
        raise ValueError("Invalid base_url: contains control characters")
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Invalid base_url scheme: {parsed.scheme}")
    if parsed.username or parsed.password:
        raise ValueError("Invalid base_url: credentials are not allowed")
    return candidate


def _capability_availability(route_kind: RouteKind) -> Dict[str, Any]:
    registry = get_registry()
    available = []
    for backend_class, config in registry.backends.items():
        if not config.supports(route_kind):
            continue
        hostname = backend_hostname(backend_class, registry=registry, fallback_base_url=config.base_url)
        entry: Dict[str, Any] = {
            "backend_class": backend_class,
            "base_url": config.base_url,
            "host": _backend_host(config.base_url),
            "description": config.description,
        }
        if hostname:
            entry["hostname"] = hostname
        try:
            from app.health_checker import get_health_checker

            checker = get_health_checker()
            status = checker.get_status(backend_class)
        except Exception:
            status = None
        if status is not None:
            entry["healthy"] = status.is_healthy
            entry["ready"] = status.is_ready
            if status.error:
                entry["health_error"] = status.error
        available.append(entry)
    available.sort(key=lambda item: item.get("backend_class") or "")
    return {
        "capability": route_kind,
        "available_backends": available,
        "available_count": len(available),
    }


class AdmissionController:
    """Enforces concurrency limits with semaphore-based admission control.

    Tracks inflight requests per (backend_class, route_kind) pair.
    Returns 429 immediately when limit is exceeded (no queueing).
    """

    def __init__(self, registry: BackendRegistry):
        self.registry = registry
        # Semaphores keyed by (backend_class, route_kind)
        self._semaphores: Dict[tuple[str, RouteKind], asyncio.Semaphore] = {}
        # Locks to make admission checks atomic per backend/route
        self._locks: Dict[tuple[str, RouteKind], asyncio.Lock] = {}
        self._init_semaphores()

    def _init_semaphores(self):
        """Initialize semaphores for all backend/route combinations."""
        for backend_class, config in self.registry.backends.items():
            for route_kind in config.supported_capabilities:
                limit = config.get_limit(route_kind)
                key = (backend_class, route_kind)
                self._semaphores[key] = asyncio.Semaphore(limit)
                setattr(self._semaphores[key], "_initial_value", limit)  # type: ignore[attr-defined]
                self._locks[key] = asyncio.Lock()
                logger.info(
                    f"Admission control: {backend_class}.{route_kind} limit={limit}"
                )

    def sync_registry(self, registry: BackendRegistry) -> None:
        self.registry = registry
        next_semaphores: Dict[tuple[str, RouteKind], asyncio.Semaphore] = {}
        next_locks: Dict[tuple[str, RouteKind], asyncio.Lock] = {}
        for backend_class, config in self.registry.backends.items():
            for route_kind in config.supported_capabilities:
                key = (backend_class, route_kind)
                limit = max(1, config.get_limit(route_kind))
                sem = self._semaphores.get(key)
                current_limit = getattr(sem, "_initial_value", None) if sem is not None else None  # type: ignore[attr-defined]
                if sem is None or current_limit != limit:
                    sem = asyncio.Semaphore(limit)
                    setattr(sem, "_initial_value", limit)  # type: ignore[attr-defined]
                next_semaphores[key] = sem
                next_locks[key] = self._locks.get(key) or asyncio.Lock()
        self._semaphores = next_semaphores
        self._locks = next_locks

    def _get_semaphore(
        self, backend_class: str, route_kind: RouteKind
    ) -> Optional[asyncio.Semaphore]:
        """Get semaphore for a backend/route pair."""
        # Resolve legacy names
        actual_class = self.registry.resolve_backend_class(backend_class)
        return self._semaphores.get((actual_class, route_kind))

    def _get_lock(self, backend_class: str, route_kind: RouteKind) -> Optional[asyncio.Lock]:
        """Get lock for a backend/route pair."""
        actual_class = self.registry.resolve_backend_class(backend_class)
        return self._locks.get((actual_class, route_kind))

    async def acquire(self, backend_class: str, route_kind: RouteKind):
        """Acquire a slot for the request. Raises HTTPException 429 if overloaded.

        This is a non-blocking check - if the semaphore is at capacity,
        we immediately fail rather than waiting.
        """
        sem = self._get_semaphore(backend_class, route_kind)
        lock = self._get_lock(backend_class, route_kind)
        if sem is None:
            # No semaphore means this backend doesn't support this route
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "capability_not_supported",
                    "backend_class": backend_class,
                    "route_kind": route_kind,
                    "message": f"Backend {backend_class} does not support {route_kind}",
                    **_capability_availability(route_kind),
                },
            )

        if lock is None:
            await sem.acquire()
            return

        # Try to acquire without blocking, guarding against race conditions.
        async with lock:
            if sem._value == 0:
                # Semaphore is at capacity
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "backend_overloaded",
                        "backend_class": backend_class,
                        "route_kind": route_kind,
                        "message": f"Backend {backend_class} is at capacity for {route_kind} requests",
                    },
                    headers={"Retry-After": "5"},
                )

            await sem.acquire()

    def release(self, backend_class: str, route_kind: RouteKind):
        """Release a slot after request completes."""
        sem = self._get_semaphore(backend_class, route_kind)
        if sem is not None:
            sem.release()

    def get_stats(self) -> Dict[str, Any]:
        """Get current admission control statistics."""
        stats = {}
        for (backend_class, route_kind), sem in self._semaphores.items():
            key = f"{backend_class}.{route_kind}"
            # Use _value for available, calculate limit from initial config
            config = self.registry.get_backend(backend_class)
            limit = config.get_limit(route_kind) if config else 1
            available = sem._value
            stats[key] = {
                "limit": limit,
                "available": available,
                "inflight": limit - available,
            }
        return stats


def load_backends_config(path: Optional[Path] = None) -> BackendRegistry:
    """Load backend configuration from YAML file."""
    if path is None:
        # Default to file in app directory
        path = Path(__file__).parent / "backends_config.yaml"

    if not path.exists():
        logger.warning(f"Backends config not found at {path}, using minimal defaults")
        return _default_registry()

    with open(path, "r") as f:
        data = yaml.safe_load(f)

    disabled_classes = {
        item.strip().lower()
        for item in str(getattr(S, "DISABLED_BACKEND_CLASSES", "") or "").split(",")
        if item.strip()
    }

    backends = {}
    for name, cfg in data.get("backends", {}).items():
        backend_class = str(cfg.get("class", name) or name).strip()
        if backend_class.lower() in disabled_classes or name.strip().lower() in disabled_classes:
            logger.info("Skipping disabled backend class %s from %s", backend_class, path)
            continue
        health = cfg.get("health", {})

        # Allow env var substitution in YAML values (e.g. "${HEARTMULA_BASE_URL}").
        raw_base_url = cfg.get("base_url", "")
        base_url = os.path.expandvars(raw_base_url) if isinstance(raw_base_url, str) else ""

        # Fallback: if placeholders remain (e.g., because env file was loaded via pydantic
        # Settings rather than placed in os.environ), substitute from Settings `S`.
        # This handles cases like "${HEARTMULA_BASE_URL}" when the process env does not
        # contain HEARTMULA_BASE_URL but `S.HEARTMULA_BASE_URL` is configured via env_file.
        try:
            import re

            def _replace_var(m: re.Match) -> str:
                name = m.group(1)
                return str(getattr(S, name, "")) or ""

            if isinstance(raw_base_url, str):
                # First, expand any env vars present
                candidate = os.path.expandvars(raw_base_url)
                # Then replace ${VAR} placeholders with values from S when available
                candidate = re.sub(r"\$\{([A-Z0-9_]+)\}", _replace_var, candidate)
                base_url = candidate
        except Exception:
            pass

        backends[name] = BackendConfig(
            backend_class=backend_class,
            provider=str(cfg.get("provider", backend_class) or backend_class or name),
            base_url=_sanitize_base_url(base_url),
            description=cfg.get("description", ""),
            supported_capabilities=cfg.get("supported_capabilities", []),
            concurrency_limits=cfg.get("concurrency_limits", {}),
            health_liveness=health.get("liveness", "/healthz"),
            health_readiness=health.get("readiness", "/readyz"),
            payload_policy=cfg.get("payload_policy", {}),
        )

    legacy_mapping = data.get("legacy_mapping", {})

    logger.info(f"Loaded {len(backends)} backend configs from {path}")
    return BackendRegistry(
        backends=dict(backends),
        legacy_mapping=legacy_mapping,
        static_backends=dict(backends),
    )


def _default_registry() -> BackendRegistry:
    """Create a minimal default registry for backward compatibility."""
    backends = {
        "local_vllm": BackendConfig(
            backend_class="local_vllm",
            provider="vllm",
            base_url=S.VLLM_BASE_URL,
            description="Default vLLM backend",
            supported_capabilities=["chat"],
            concurrency_limits={"chat": 8},
            health_liveness="/models",
            health_readiness="/models",
            payload_policy={},
        ),
        "local_vllm_fast": BackendConfig(
            backend_class="local_vllm_fast",
            provider="vllm",
            base_url=S.VLLM_FAST_BASE_URL,
            description="Fast vLLM backend",
            supported_capabilities=["chat"],
            concurrency_limits={"chat": 8},
            health_liveness="/models",
            health_readiness="/models",
            payload_policy={},
        ),
        "local_vllm_embeddings": BackendConfig(
            backend_class="local_vllm_embeddings",
            provider="vllm",
            base_url=S.VLLM_EMBEDDINGS_BASE_URL,
            description="vLLM embeddings backend",
            supported_capabilities=["embeddings"],
            concurrency_limits={"embeddings": 8},
            health_liveness="/models",
            health_readiness="/models",
            payload_policy={},
        ),
        "local_mlx": BackendConfig(
            backend_class="local_mlx",
            provider="mlx",
            base_url=S.MLX_BASE_URL,
            description="Optional MLX backend",
            supported_capabilities=["chat", "embeddings"],
            concurrency_limits={"chat": 2, "embeddings": 2},
            health_liveness="/models",
            health_readiness="/models",
            payload_policy={},
        ),
    }
    return BackendRegistry(
        backends=dict(backends),
        legacy_mapping={
            "vllm": "local_vllm",
            "vllm_fast": "local_vllm_fast",
            "vllm_embeddings": "local_vllm_embeddings",
            "mlx": "local_mlx",
            "ollama": "local_vllm",
        },
        static_backends=dict(backends),
    )


# Global registry and admission controller
_registry: Optional[BackendRegistry] = None
_admission: Optional[AdmissionController] = None


def init_backends():
    """Initialize backend registry and admission controller. Call at startup."""
    global _registry, _admission
    _registry = load_backends_config()
    _admission = AdmissionController(_registry)
    logger.info("Backend registry and admission control initialized")


def get_registry() -> BackendRegistry:
    """Get the global backend registry."""
    if _registry is None:
        # Auto-initialize with defaults if not explicitly initialized
        init_backends()
    return _registry


def get_admission_controller() -> AdmissionController:
    """Get the global admission controller."""
    if _admission is None:
        # Auto-initialize with defaults if not explicitly initialized
        init_backends()
    return _admission


async def check_capability(backend_class: str, route_kind: RouteKind):
    """Check if a backend supports a capability. Raises HTTPException if not."""
    registry = get_registry()
    backend = registry.get_backend(backend_class)

    if backend is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "backend_not_found",
                "backend_class": backend_class,
                "message": f"Backend {backend_class} is not configured",
                **_capability_availability(route_kind),
            },
        )

    if not backend.supports(route_kind):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "capability_not_supported",
                "backend_class": backend_class,
                "route_kind": route_kind,
                "message": f"Backend {backend_class} does not support {route_kind}",
                "supported_capabilities": backend.supported_capabilities,
                **_capability_availability(route_kind),
            },
        )
