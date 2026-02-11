from __future__ import annotations

from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import httpx

from app.config import S, logger
from app.openai_utils import new_id, now_unix
from app.request_log import StreamMetrics, write_request_event
from app.health_routes import router as health_router
from app.memory_legacy import memory_init
from app.memory_routes import router as memory_router
from app.openai_routes import router as openai_router
from app.model_aliases import get_aliases
from app.tools_bus import router as tools_router
from app.agent_routes import router as agent_router
from app.ui_routes import router as ui_router
from app.images_routes import router as images_router
from app.music_routes import router as music_router
from app import memory_v2
from app import metrics
from app import user_store
from app.observability_server import ObservabilityServer


async def _startup_check_models() -> None:
    """Non-fatal checks to catch common misconfigurations early."""

    # Warn if Ollama-backed aliases point at model tags that aren't present.
    try:
        aliases = get_aliases()
        wanted = sorted({a.upstream_model for a in aliases.values() if a.backend == "ollama" and a.upstream_model})
        if not wanted:
            return

        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
            # If Ollama isn't reachable, don't spam logs; this can happen on cold boot.
            if r.status_code != 200:
                logger.info("startup: ollama /api/tags status=%s (skipping model check)", r.status_code)
                return

            payload = r.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            present = set()
            if isinstance(models, list):
                for m in models:
                    if isinstance(m, dict) and isinstance(m.get("name"), str):
                        present.add(m["name"])

        missing = [m for m in wanted if m not in present]
        for m in missing:
            logger.warning("startup: ollama model missing: %s (check model_aliases.json or run 'ollama pull %s')", m, m)
    except Exception as e:
        logger.info("startup: model availability check skipped (%s: %s)", type(e).__name__, e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Initialize backend registry and admission control
    from app.backends import init_backends
    from app.health_checker import init_health_checker, start_health_checker, stop_health_checker
    
    init_backends()
    init_health_checker()
    observability = ObservabilityServer()
    observability.start()
    try:
        user_store.init_db(S.USER_DB_PATH)
    except Exception as e:
        logger.warning("startup: failed to init user db (%s: %s)", type(e).__name__, e)
    
    # Start background health checking
    await start_health_checker()
    
    await _startup_check_models()
    yield
    
    # Stop health checker on shutdown
    await stop_health_checker()
    observability.stop()


app = FastAPI(title="Local AI Gateway", version="0.1", lifespan=lifespan)


# Minimal static UI assets (served without auth; API endpoints remain bearer-protected).
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def guard_requests(req: Request, call_next):
    try:
        max_bytes = int(getattr(S, "MAX_REQUEST_BYTES", 0) or 0)
    except Exception:
        max_bytes = 0

    # Optional per-token override from policy JSON.
    try:
        from app.auth import bearer_token_from_headers, token_policy_for_token

        tok = bearer_token_from_headers(dict(req.headers))
        if tok:
            pol = token_policy_for_token(tok)
            if isinstance(pol, dict) and pol.get("max_request_bytes") is not None:
                try:
                    max_bytes = int(pol.get("max_request_bytes") or 0)
                except Exception:
                    pass
    except Exception:
        pass

    if max_bytes > 0:
        # Prefer content-length when present.
        try:
            cl = req.headers.get("content-length")
            if cl is not None and int(cl) > max_bytes:
                from fastapi.responses import PlainTextResponse

                return PlainTextResponse("request too large", status_code=413)
        except Exception:
            pass

        # If no content-length, fall back to reading body for typical body methods.
        if req.method.upper() in {"POST", "PUT", "PATCH"}:
            try:
                body = await req.body()
                if body is not None and len(body) > max_bytes:
                    from fastapi.responses import PlainTextResponse

                    return PlainTextResponse("request too large", status_code=413)
            except Exception:
                pass

    return await call_next(req)


@app.middleware("http")
async def log_requests(req: Request, call_next):
    start_wall = time.time()
    start_monotonic = time.monotonic()
    request_id = new_id("req")
    req.state.request_id = request_id
    req.state.instrument = getattr(req.state, "instrument", {})
    resp = None
    did_wrap_stream = False
    try:
        resp = await call_next(req)

        # Correlation header for clients and log lookup.
        try:
            resp.headers["X-Request-Id"] = request_id
        except Exception:
            pass

        # Streaming responses must be instrumented during iteration.
        content_type = ""
        try:
            content_type = (resp.headers.get("content-type") or "").lower()
        except Exception:
            content_type = ""
        is_streaming = content_type.startswith("text/event-stream") and hasattr(resp, "body_iterator")
        if not is_streaming:
            is_streaming = isinstance(resp, StreamingResponse) or (
                getattr(resp, "media_type", None) == "text/event-stream" and hasattr(resp, "body_iterator")
            )
        if is_streaming:
            orig_iter = resp.body_iterator
            metrics = StreamMetrics(started_monotonic=start_monotonic)
            did_wrap_stream = True

            async def _wrap():
                try:
                    async for chunk in orig_iter:
                        if isinstance(chunk, (bytes, bytearray)):
                            metrics.on_chunk(bytes(chunk))
                        yield chunk
                except Exception as e:
                    metrics.abort_reason = f"{type(e).__name__}: {e}"
                    raise
                finally:
                    base = {
                        "ts": now_unix(),
                        "request_id": request_id,
                        "method": req.method,
                        "path": req.url.path,
                        "status": resp.status_code,
                    }
                    extra = getattr(req.state, "instrument", None)
                    if isinstance(extra, dict):
                        base.update(extra)
                    base.update(metrics.finish())
                    write_request_event(base)

            resp.body_iterator = _wrap()

        return resp
    finally:
        dur_ms = (time.time() - start_wall) * 1000.0
        status = resp.status_code if resp is not None else 500
        logger.info("%s %s -> %d (%.1fms)", req.method, req.url.path, status, dur_ms)

        # Best-effort metrics.
        try:
            if getattr(S, "METRICS_ENABLED", True):
                metrics.observe_request(req.url.path, status, dur_ms)
        except Exception:
            pass

        # Non-stream requests: log immediately.
        if resp is not None and not did_wrap_stream:
            base = {
                "ts": now_unix(),
                "request_id": request_id,
                "method": req.method,
                "path": req.url.path,
                "status": resp.status_code,
                "stream": False,
                "duration_ms": round((time.monotonic() - start_monotonic) * 1000.0, 1),
            }
            extra = getattr(req.state, "instrument", None)
            if isinstance(extra, dict):
                base.update(extra)
            write_request_event(base)


# One-time DB init
memory_init()
if S.MEMORY_V2_ENABLED:
    memory_v2.init(S.MEMORY_DB_PATH)


# Routers
app.include_router(health_router)
app.include_router(openai_router)
app.include_router(images_router)
app.include_router(music_router)
from app.tts_routes import router as tts_router
app.include_router(tts_router)
# Proxy audio from HeartMula through the gateway (e.g., /ui/heartmula/audio/{filename})
from app.audio_routes import router as audio_router
app.include_router(audio_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(agent_router)
app.include_router(ui_router)
