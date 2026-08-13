from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import S


_CONNECT_TIMEOUT_BUDGET_SEC = 30.0
_POOL_TIMEOUT_CAP_SEC = 30.0


def _connect_attempts(connect_retries: int) -> int:
    return max(1, int(connect_retries or 0) + 1)


def _connect_budget_sec(timeout: float | None) -> float | None:
    """Return the aggregate connect budget only when the long-timeout cap applies."""
    if timeout is None:
        return None
    total = max(0.001, float(timeout))
    if total <= _CONNECT_TIMEOUT_BUDGET_SEC:
        return None
    return _CONNECT_TIMEOUT_BUDGET_SEC


def _effective_timeout(
    timeout: float | None,
    *,
    connect_retries: int = 0,
) -> httpx.Timeout | None:
    """Preserve read/write budgets and cap long connection waits across retries."""
    if timeout is None:
        return None
    total = max(0.001, float(timeout))
    connect_limit = min(total, _CONNECT_TIMEOUT_BUDGET_SEC)
    connect_budget = _connect_budget_sec(timeout)
    if connect_budget is not None:
        connect_limit = connect_budget / _connect_attempts(connect_retries)
    return httpx.Timeout(
        total,
        connect=connect_limit,
        pool=min(total, _POOL_TIMEOUT_CAP_SEC),
    )


def _client_timeout_value(requested: float | None) -> float | None:
    """Preserve the shared client's legacy scalar timeout configuration."""
    if requested is None:
        return None
    return max(0.001, float(requested))


def _timeout_extension(timeout: httpx.Timeout | None) -> dict[str, float | None] | None:
    if timeout is None:
        return None
    return {
        "connect": timeout.connect,
        "read": timeout.read,
        "write": timeout.write,
        "pool": timeout.pool,
    }


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
    connect_attempts: int = 1,
    connect_budget_sec: float | None = None,
) -> None:
    """Ensure blank RequestError messages retain useful diagnostic text.

    httpx timeout subclasses may stringify to an empty string when their
    underlying transport exception did not carry a message. Upstream adapters
    historically persisted ``str(exc)``, which turned these failures into
    ``error: ''`` in Coding Workspace debug reports. Keep non-empty upstream
    messages untouched; for blank messages, synthesize the exception class and,
    when available, the timeout phase and effective limit.
    """
    if str(exc).strip():
        return
    error_type = type(exc).__name__
    phase = _timeout_phase(exc)
    limit = _phase_timeout_sec(timeout, phase)
    if phase == "connect" and limit is not None and connect_attempts > 1 and connect_budget_sec is not None:
        message = (
            f"{error_type}: connect timeout after {_format_seconds(limit)}s per attempt "
            f"({_format_seconds(connect_budget_sec)}s budget across {connect_attempts} attempts)"
        )
    elif phase and limit is not None:
        message = f"{error_type}: {phase} timeout after {_format_seconds(limit)}s"
    elif phase:
        message = f"{error_type}: {phase} timeout"
    else:
        message = error_type
    exc.args = (message,)


def _instrument_client_send(
    client: Any,
    *,
    timeout: httpx.Timeout | None,
    connect_attempts: int = 1,
    connect_budget_sec: float | None = None,
) -> None:
    """Apply diagnostic phase timeouts without changing the returned client type."""
    original_send = getattr(client, "send", None)
    if not callable(original_send):
        return
    timeout_extension = _timeout_extension(timeout)

    async def diagnostic_send(request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        # AsyncClient.send populates the timeout extension only when it is
        # absent. Set the phase-specific values first so long-timeout transport
        # retries share the capped aggregate connect budget, while retaining
        # short-timeout behavior and the scalar client configuration expected by
        # existing callers and tests. Respect explicit per-request overrides.
        if timeout_extension is not None and "timeout" not in request.extensions:
            request.extensions["timeout"] = dict(timeout_extension)
        try:
            return await original_send(request, *args, **kwargs)
        except httpx.RequestError as exc:
            _ensure_request_error_text(
                exc,
                timeout=timeout,
                connect_attempts=connect_attempts,
                connect_budget_sec=connect_budget_sec,
            )
            raise

    client.send = diagnostic_send


@asynccontextmanager
async def httpx_client(*, timeout: float | None = None):
    """Create an httpx.AsyncClient configured by gateway backend TLS settings.

    Honors upstream TLS settings and retries connection establishment failures.
    Long generation/read budgets are preserved; when the 30-second connection
    cap applies, that aggregate budget is divided across transport retry
    attempts. Short timeout semantics are preserved, and pool waits are capped.
    Blank request-error messages are enriched with exception class and timeout
    phase/limit details for downstream diagnostics; non-empty messages are kept.
    """
    kwargs: dict[str, object] = {}
    connect_retries = max(0, int(getattr(S, "BACKEND_CONNECT_RETRIES", 2) or 0))
    transport_kwargs: dict[str, object] = {
        "retries": connect_retries,
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
    effective_timeout = _effective_timeout(timeout, connect_retries=connect_retries)
    client_timeout = _client_timeout_value(timeout)
    attempts = _connect_attempts(connect_retries)

    async with httpx.AsyncClient(timeout=client_timeout, **kwargs) as client:
        _instrument_client_send(
            client,
            timeout=effective_timeout,
            connect_attempts=attempts,
            connect_budget_sec=_connect_budget_sec(timeout),
        )
        yield client
