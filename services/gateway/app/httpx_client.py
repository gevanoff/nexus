from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from app.config import S


@asynccontextmanager
async def httpx_client(*, timeout: float | None = None):
    """Create an httpx.AsyncClient configured by gateway backend TLS settings.

    Honors upstream TLS settings and retries connection establishment failures.
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

    async with httpx.AsyncClient(timeout=timeout, **kwargs) as client:
        yield client
