from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from app.config import S


@asynccontextmanager
async def httpx_client(*, timeout: float | None = None):
    """Create an httpx.AsyncClient configured by gateway backend TLS settings.

    Honors S.BACKEND_VERIFY_TLS, S.BACKEND_CA_BUNDLE, and S.BACKEND_CLIENT_CERT.
    """
    kwargs: dict[str, object] = {}
    # verify can be True/False or a path to a CA bundle
    if S.BACKEND_CA_BUNDLE:
        kwargs["verify"] = S.BACKEND_CA_BUNDLE
    else:
        kwargs["verify"] = bool(S.BACKEND_VERIFY_TLS)

    if S.BACKEND_CLIENT_CERT:
        parts = [p.strip() for p in S.BACKEND_CLIENT_CERT.split(",") if p.strip()]
        if len(parts) == 1:
            kwargs["cert"] = parts[0]
        elif len(parts) >= 2:
            kwargs["cert"] = (parts[0], parts[1])

    async with httpx.AsyncClient(timeout=timeout, **kwargs) as client:
        yield client
