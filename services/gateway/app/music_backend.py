from __future__ import annotations

import time
from typing import Any, Dict

import httpx
from app.httpx_client import httpx_client as _httpx_client

from app.backends import get_registry
from app.config import S


def _effective_heartmula_base_url(*, backend_class: str) -> str:
    # Prefer configured backend registry (supports env var expansion via backends_config.yaml).
    try:
        reg = get_registry()
        cfg = reg.get_backend(backend_class)
        if cfg and isinstance(cfg.base_url, str) and cfg.base_url.strip():
            return cfg.base_url.strip().rstrip("/")
    except Exception:
        pass

    # Fallback to Settings.
    return (getattr(S, "HEARTMULA_BASE_URL", "") or "").strip().rstrip("/")


def _effective_timeout_sec() -> float:
    try:
        return float(getattr(S, "HEARTMULA_TIMEOUT_SEC", 120.0) or 120.0)
    except Exception:
        return 120.0


def _effective_generate_path() -> str:
    p = (getattr(S, "HEARTMULA_GENERATE_PATH", "") or "/v1/music/generations").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


async def generate_music(*, backend_class: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Proxy a music generation request to HeartMula.

    This is intentionally a mostly-transparent proxy because HeartMula's API
    surface may evolve. The gateway adds a stable `_gateway` envelope for
    debugging/telemetry.
    """

    base = _effective_heartmula_base_url(backend_class=backend_class)
    if not base:
        raise RuntimeError("HEARTMULA_BASE_URL is required (or set base_url for heartmula_music in backends_config.yaml)")

    timeout = _effective_timeout_sec()
    # If the caller provided a duration, increase timeout heuristically to allow longer generations.
    # Use a conservative multiplier: 5s per second of audio plus a 30s buffer.
    try:
        dur = float(body.get("duration", 0) or 0)
        if dur > 0:
            suggested = dur * 5.0 + 30.0
            if suggested > timeout:
                timeout = suggested
    except Exception:
        # Ignore if duration isn't a number
        pass

    path = _effective_generate_path()

    # Best-effort: allow callers to send "input" instead of "prompt".
    if "prompt" not in body and isinstance(body.get("input"), str):
        body = dict(body)
        body["prompt"] = body.get("input")

    # Normalize tags: if it's a list, join with commas; if not a string, convert or default.
    tags = body.get("tags")
    if isinstance(tags, list):
        body = dict(body)
        body["tags"] = ",".join(str(t) for t in tags if t)
    elif tags is not None and not isinstance(tags, str):
        body = dict(body)
        body["tags"] = str(tags)

    started = time.time()
    async with _httpx_client(timeout=timeout) as client:
        r = await client.post(f"{base}{path}", json=body)

    # Raise on non-2xx, but keep detail readable.
    if r.status_code < 200 or r.status_code >= 300:
        detail: Any
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"heartmula HTTP {r.status_code}: {detail}")

    try:
        out = r.json()
    except Exception:
        # If HeartMula returns non-JSON, wrap it.
        out = {"raw": r.text}

    # Ensure stable envelope for UI/debugging.
    if isinstance(out, dict):
        # If HeartMula returned a local audio path like "/audio/<id>.wav", convert it to a gateway-proxied URL
        # so the browser fetches audio through the gateway rather than directly from HeartMula.
        audio_url = out.get("audio_url")
        if isinstance(audio_url, str) and audio_url.startswith("/"):
            upstream = f"{base}{audio_url}"
            # Use basename for a stable proxy path: /ui/heartmula/audio/{filename}
            from urllib.parse import urlparse

            parsed = urlparse(upstream)
            filename = parsed.path.rsplit("/", 1)[-1]
            if filename:
                out["audio_url"] = f"/ui/heartmula/audio/{filename}"
                # Preserve the original upstream URL for debugging
                out.setdefault("_gateway", {}).update({"upstream_audio_url": upstream})
            else:
                # Fallback: expose the absolute upstream url
                out["audio_url"] = upstream

        gw = out.get("_gateway")
        if not isinstance(gw, dict):
            gw = {}
        gw.update(
            {
                "backend": "heartmula",
                "backend_class": backend_class,
                "upstream_base_url": base,
                "upstream_path": path,
                "upstream_latency_ms": round((time.time() - started) * 1000.0, 1),
            }
        )
        out["_gateway"] = gw

    return out
