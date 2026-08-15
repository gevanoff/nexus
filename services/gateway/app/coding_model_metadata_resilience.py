from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable, Dict, Optional
from urllib import error as urlerror

from app import coding_network_resilience as network


_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _exception_kind(exc: Exception) -> str:
    if isinstance(exc, urlerror.HTTPError):
        try:
            if int(exc.code) in _RETRYABLE_HTTP_STATUSES:
                return "http_status"
        except Exception:
            return ""
        return ""
    kind = network.classify_transient_text(f"{type(exc).__name__}: {exc}")
    if kind:
        return kind
    if isinstance(exc, (urlerror.URLError, TimeoutError, ConnectionError)):
        return "connect"
    return ""


def fetch_metadata_with_retry(
    original: Callable[..., Dict[str, Any]],
    model_id: str,
    *,
    timeout_sec: float = 10.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    attempts: Optional[int] = None,
    base_delay_sec: Optional[float] = None,
) -> Dict[str, Any]:
    max_attempts = network.retry_attempts() if attempts is None else max(1, int(attempts))
    base_delay = network.retry_base_delay_sec() if base_delay_sec is None else max(0.0, float(base_delay_sec))
    last_exc: Exception | None = None

    for index in range(max_attempts):
        try:
            return original(model_id, timeout_sec=timeout_sec)
        except Exception as exc:
            last_exc = exc
            kind = _exception_kind(exc)
            if not kind or index + 1 >= max_attempts:
                raise
            delay = min(30.0, base_delay * (2**index))
            if delay > 0:
                sleep_fn(delay)

    if last_exc is not None:  # pragma: no cover - loop always returns or raises
        raise last_exc
    return {}  # pragma: no cover


def install(model_integration_workspace: Any) -> None:
    if bool(getattr(model_integration_workspace, "_coding_metadata_resilience_installed", False)):
        return

    original = model_integration_workspace.fetch_model_metadata

    @wraps(original)
    def resilient_fetch_model_metadata(model_id: str, *, timeout_sec: float = 10.0) -> Dict[str, Any]:
        return fetch_metadata_with_retry(
            original,
            model_id,
            timeout_sec=timeout_sec,
        )

    model_integration_workspace._network_resilience_original_fetch_model_metadata = original
    model_integration_workspace.fetch_model_metadata = resilient_fetch_model_metadata
    model_integration_workspace._coding_metadata_resilience_installed = True
