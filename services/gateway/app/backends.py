"""Backend configuration and admission control.

Single source of truth for backend classes, capabilities, concurrency limits,
and payload policies. Provides deterministic routing and fast-fail overload protection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastapi import HTTPException

from app.config import S, logger


RouteKind = Literal["chat", "embeddings", "images", "music", "tts"]


@dataclass(frozen=True)
class BackendConfig:
    """Configuration for a single backend class."""

    backend_class: str
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


@dataclass
class BackendRegistry:
    """Registry of all backend configurations."""

    backends: Dict[str, BackendConfig]
    legacy_mapping: Dict[str, str]

    def get_backend(self, backend_class: str) -> Optional[BackendConfig]:
        """Get backend config by class name."""
        # Check legacy mapping first
        actual_class = self.legacy_mapping.get(backend_class, backend_class)
        return self.backends.get(actual_class)

    def resolve_backend_class(self, backend_name: str) -> str:
        """Resolve a backend name (including legacy names) to its canonical class."""
        return self.legacy_mapping.get(backend_name, backend_name)


def _backend_host(base_url: str) -> Optional[str]:
    try:
        parsed = urlparse(base_url or "")
    except Exception:
        return None
    host = parsed.hostname or parsed.netloc
    if not host:
        return None
    return host


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
        entry: Dict[str, Any] = {
            "backend_class": backend_class,
            "base_url": config.base_url,
            "host": _backend_host(config.base_url),
            "description": config.description,
        }
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
                self._locks[key] = asyncio.Lock()
                logger.info(
                    f"Admission control: {backend_class}.{route_kind} limit={limit}"
                )

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

    backends = {}
    for name, cfg in data.get("backends", {}).items():
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
            backend_class=cfg.get("class", name),
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
    return BackendRegistry(backends=backends, legacy_mapping=legacy_mapping)


def _default_registry() -> BackendRegistry:
    """Create a minimal default registry for backward compatibility."""
    backends = {
        "ollama": BackendConfig(
            backend_class="ollama",
            base_url=S.OLLAMA_BASE_URL,
            description="Default Ollama backend",
            supported_capabilities=["chat", "embeddings"],
            concurrency_limits={"chat": 4, "embeddings": 4},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={},
        ),
        "mlx": BackendConfig(
            backend_class="mlx",
            base_url=S.MLX_BASE_URL,
            description="Default MLX backend",
            supported_capabilities=["chat", "embeddings"],
            concurrency_limits={"chat": 2, "embeddings": 2},
            health_liveness="/healthz",
            health_readiness="/readyz",
            payload_policy={},
        ),
    }
    return BackendRegistry(backends=backends, legacy_mapping={})


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
