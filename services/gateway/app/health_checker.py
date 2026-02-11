"""Backend health and readiness checking.

Periodically checks each backend's /healthz and /readyz endpoints
to ensure requests aren't routed to unhealthy backends.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

from app.config import logger
from app.httpx_client import httpx_client as _httpx_client
from app.backends import BackendConfig, RouteKind, _backend_host, _capability_availability, get_registry


@dataclass
class HealthStatus:
    """Health status for a single backend."""
    
    backend_class: str
    is_healthy: bool
    is_ready: bool
    last_check: float
    error: Optional[str] = None


class HealthChecker:
    """Periodically checks backend health and readiness."""
    
    def __init__(self, check_interval: float = 30.0, timeout: float = 5.0):
        self.check_interval = check_interval
        self.timeout = timeout
        self._status: Dict[str, HealthStatus] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self):
        """Start background health checking."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("Health checker started")
    
    async def stop(self):
        """Stop background health checking."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Health checker stopped")
    
    async def _check_loop(self):
        """Background loop that periodically checks all backends."""
        while self._running:
            try:
                await self._check_all()
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
            
            await asyncio.sleep(self.check_interval)
    
    async def _check_all(self):
        """Check all backends concurrently."""
        registry = get_registry()
        
        tasks = []
        for backend_class, config in registry.backends.items():
            tasks.append(self._check_backend(backend_class, config))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _check_backend(self, backend_class: str, config: BackendConfig):
        """Check a single backend's health and readiness."""
        base_url = (config.base_url or "").rstrip("/")
        
        is_healthy = False
        is_ready = False
        error = None
        
        # If base_url isn't configured (e.g. env-var placeholder not set), mark not-ready.
        if not base_url or not (base_url.startswith("http://") or base_url.startswith("https://")):
            status = HealthStatus(
                backend_class=backend_class,
                is_healthy=False,
                is_ready=False,
                last_check=time.time(),
                error="base_url not configured",
            )
            self._status[backend_class] = status
            return

        try:
            async with _httpx_client(timeout=self.timeout) as client:
                # Check liveness
                try:
                    health_resp = await client.get(f"{base_url}{config.health_liveness}")
                    is_healthy = health_resp.status_code == 200
                except Exception as e:
                    error = f"liveness check failed: {e}"

                # Check readiness (only if healthy)
                if is_healthy:
                    try:
                        ready_resp = await client.get(f"{base_url}{config.health_readiness}")
                        is_ready = ready_resp.status_code == 200
                    except Exception as e:
                        error = f"readiness check failed: {e}"
        except Exception as e:
            error = f"health check error: {e}"
        
        status = HealthStatus(
            backend_class=backend_class,
            is_healthy=is_healthy,
            is_ready=is_ready,
            last_check=time.time(),
            error=error,
        )
        
        self._status[backend_class] = status
        
        if not is_ready:
            logger.warning(
                f"Backend {backend_class} not ready: healthy={is_healthy}, ready={is_ready}, error={error}"
            )
    
    def get_status(self, backend_class: str) -> Optional[HealthStatus]:
        """Get current health status for a backend."""
        return self._status.get(backend_class)
    
    def is_ready(self, backend_class: str) -> bool:
        """Check if a backend is ready to accept requests.
        
        Returns True if:
        - No health check has run yet (optimistic start)
        - Backend is marked as ready
        """
        status = self._status.get(backend_class)
        if status is None:
            # No check yet, be optimistic
            return True
        
        return status.is_ready
    
    def get_all_status(self) -> Dict[str, HealthStatus]:
        """Get status for all backends."""
        return dict(self._status)


# Global health checker instance
_health_checker: Optional[HealthChecker] = None


def init_health_checker():
    """Initialize the global health checker. Call at startup."""
    global _health_checker
    _health_checker = HealthChecker()
    logger.info("Health checker initialized")


async def start_health_checker():
    """Start background health checking."""
    if _health_checker:
        await _health_checker.start()


async def stop_health_checker():
    """Stop background health checking."""
    if _health_checker:
        await _health_checker.stop()


def get_health_checker() -> HealthChecker:
    """Get the global health checker."""
    if _health_checker is None:
        # Auto-initialize for tests/contexts where init wasn't called
        init_health_checker()
    return _health_checker


def check_backend_ready(backend_class: str, route_kind: RouteKind | None = None):
    """Check if a backend is ready. Raises HTTPException if not.
    
    This is called before routing requests to ensure the backend
    can actually handle them. In tests or before first health check,
    optimistically assumes ready.
    """
    from fastapi import HTTPException
    
    try:
        checker = get_health_checker()
    except Exception:
        # Health checker not available (tests, early startup)
        return
    
    registry = get_registry()
    backend = registry.get_backend(backend_class)
    if backend is None:
        detail = {
            "error": "backend_not_found",
            "backend_class": backend_class,
            "message": f"Backend {backend_class} is not configured",
        }
        if route_kind is not None:
            detail.update(_capability_availability(route_kind))
        raise HTTPException(status_code=400, detail=detail)

    if not checker.is_ready(backend_class):
        status = checker.get_status(backend_class)
        detail = {
            "error": "backend_not_ready",
            "backend_class": backend_class,
            "message": f"Backend {backend_class} is not ready to accept requests",
            "backend": {
                "backend_class": backend.backend_class,
                "base_url": backend.base_url,
                "host": _backend_host(backend.base_url),
                "description": backend.description,
            },
        }
        if status and status.error:
            detail["health_error"] = status.error
        if status:
            detail["backend"]["healthy"] = status.is_healthy
            detail["backend"]["ready"] = status.is_ready
        if route_kind is not None:
            detail.update(_capability_availability(route_kind))
        
        raise HTTPException(
            status_code=503,
            detail=detail,
            headers={"Retry-After": "30"},
        )
