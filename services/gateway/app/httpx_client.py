from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import S


_CONNECT_TIMEOUT_CAP_SEC = 30.0
_POOL_TIMEOUT_CAP_SEC = 30.0


def _effective_timeout(timeout: float | None) -> httpx.Timeout | None:
    """Preserve long read/write budgets without allowing connection waits to inherit them."""
    if timeout is None:
        return None
    total = max(0.001, float(timeout))
    return httpx.Timeout(
        total,
        connect=min(total, _CONNECT_TIMEOUT_CAP_SEC),
        pool=min(total, _POOL_TIMEOUT_CAP_SEC),
    )


def _client_timeout_value(
    requested: float | None,
    effective: httpx.Timeout | None,
) -> float | httpx.Timeout | None:
    """Keep the legacy scalar timeout shape when all phases have the same limit."""
    if requested is None:
        return None
    total = max(0.001, float(requested))
    if total <= min(_CONNECT_TIMEOUT_CAP_SEC, _POOL_TIMEOUT_CAP_SEC):
        return total
    return effective


def _timeout_phase(exc: httpx.RequestError) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect"
    if isinstance(exc, httpx.ReadTimeout):
        return "read"
    if isinstance(exc, httpx.WriteTimeout):
        return "write"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool"
    return ""


def _phase_timeout_sec(timeout: httpx.Timeout | None, phase: str) -> float | None:
    if timeout is None or not phase:
        return None
    value = getattr(timeout, phase, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_seconds(value: float) -> str:
    return f"{value:g}"


def _ensure_request_error_text(
    exc: httpx.RequestError,
    *,
    timeout: httpx.Timeout | None,
) -> None:
    """Ensure RequestError stringification never loses the failure class.

    httpx timeout subclasses may stringify to an empty string when their
    underlying transport exception did not carry a message. Upstream adapters
    historically persisted ``str(exc)``, which turned these failures into
    ``error: ''`` in Coding Workspace debug reports. Keep non-empty upstream
    messages untouched and synthesize only the missing diagnostic text.
    """
    if str(exc).strip():
        return
    error_type = type(exc).__name__
    phase = _timeout_phase(exc)
    limit = _phase_timeout_sec(timeout, phase)
    if phase and limit is not None:
        message = f"{error_type}: {phase} timeout after {_format_seconds(limit)}s"
    elif phase:
        message = f"{error_type}: {phase} timeout"
    else:
        message = error_type
    exc.args = (message,)


def _instrument_client_send(client: Any, *, timeout: httpx.Timeout | None) -> None:
    """Wrap a real AsyncClient send method without changing the returned client type."""
    original_send = getattr(client, "send", None)
    if not callable(original_send):
        return

    async def diagnostic_send(request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            return await original_send(request, *args, **kwargs)
        except httpx.RequestError as exc:
            _ensure_request_error_text(exc, timeout=timeout)
            raise

    client.send = diagnostic_send


@asynccontextmanager
async def httpx_client(*, timeout: float | None = None):
    """Create an httpx.AsyncClient configured by gateway backend TLS settings.

    Honors upstream TLS settings and retries connection establishment failures.
    Long generation/read budgets are preserved, while connect and connection-
    pool waits are capped so an unreachable backend cannot consume the full
    generation timeout. Request errors are also guaranteed to retain a useful
    exception class and timeout phase for downstream diagnostics.
    """
    kwargs: dict[str, object] = {}
    transport_kwargs: dict[str, object] = {
        "retries": max(0, int(getattr(S, "BACKEND_CONNECT_RETRIES", 2) or 0)),
    }
    # verify can be True/False or a path to a CA bundle
    if S.BACKEND_CA_BUNDLE:
        transport_kwargs["verify"] = S.BACKEND_CA_BUNDLE
    else:
        transport_kwargs["verify"] = bool(S.BACKEND_VERIFY_TLS)

    if S.BACKEND_CLIENT_CERT:
        parts = [p.strip() for p in S.BACKEND_CLIENT_CERT.split(",") if p.strip()]
        if len(parts) == 1:
            transport_kwargs["cert"] = parts[0]
        elif len(parts) >= 2:
            transport_kwargs["cert"] = (parts[0], parts[1])

    kwargs["transport"] = httpx.AsyncHTTPTransport(**transport_kwargs)
    effective_timeout = _effective_timeout(timeout)
    client_timeout = _client_timeout_value(timeout, effective_timeout)

    async with httpx.AsyncClient(timeout=client_timeout, **kwargs) as client:
        _instrument_client_send(client, timeout=effective_timeout)
        yield client
