from __future__ import annotations

import json
import re
import time
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import require_bearer
from app.config import S, logger
from app.backends import check_capability, get_admission_controller, get_registry, llm_backends
from app.health_checker import check_backend_ready
from app.models import (
    ChatCompletionRequest,
    ChatMessage,
    CompletionRequest,
    EmbeddingsRequest,
    RerankRequest,
)
from app.openai_utils import new_id, now_unix, sse_done
from app.model_aliases import get_aliases
from app.router import decide_route
from app.router_cfg import router_cfg
from app.tool_loop import tool_loop
from app.tools_bus import allowed_tool_names_for_policy
from app.upstreams import (
    backend_model_id,
    call_backend_chat,
    default_embeddings_model_for_backend,
    embed_backend,
    stream_backend_chat_as_openai,
)
from app.memory_routes import inject_memory
from app import memory_v2


router = APIRouter()


_ALIAS_IN_REASON = re.compile(r"\balias:([a-z0-9_\-]+)\b", re.IGNORECASE)


def _selected_alias_name(request_model: str, route_reason: str) -> Optional[str]:
    aliases = get_aliases()
    key = (request_model or "").strip().lower()
    if key and key in aliases:
        return key
    m = _ALIAS_IN_REASON.search(route_reason or "")
    if m:
        cand = (m.group(1) or "").strip().lower()
        if cand in aliases:
            return cand
    return None


def _apply_alias_constraints(cc: ChatCompletionRequest, *, alias_name: Optional[str]) -> ChatCompletionRequest:
    if not alias_name:
        return cc

    a = get_aliases().get(alias_name)
    if not a:
        return cc

    # Enforce allow_tools constraint if present.
    if cc.tools and a.tools is False:
        raise HTTPException(status_code=400, detail=f"tools not allowed for model alias '{alias_name}'")

    temperature = cc.temperature
    if temperature is not None and a.temperature_cap is not None:
        temperature = min(float(temperature), float(a.temperature_cap))

    max_tokens = cc.max_tokens
    if max_tokens is not None and a.max_tokens_cap is not None:
        max_tokens = min(int(max_tokens), int(a.max_tokens_cap))

    if temperature == cc.temperature and max_tokens == cc.max_tokens:
        return cc

    return ChatCompletionRequest(
        model=cc.model,
        messages=cc.messages,
        tools=cc.tools,
        tool_choice=cc.tool_choice,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=cc.stream,
    )


def _normalize_embeddings_request_model(request_model: Optional[str], backend: str) -> str:
    resolved_backend = get_registry().resolve_backend_class(backend) or backend
    model = (request_model or "").strip()
    if not model or model.lower() == "default":
        return default_embeddings_model_for_backend(resolved_backend)

    aliases = get_aliases()
    alias = aliases.get(model.lower())
    if alias:
        alias_backend = get_registry().resolve_backend_class(alias.backend) or alias.backend
        if alias_backend == resolved_backend and (alias.upstream_model or "").strip():
            return alias.upstream_model

    if ":" in model:
        prefix, upstream_model = model.split(":", 1)
        prefix_backend = get_registry().resolve_backend_class(prefix.strip()) or prefix.strip()
        if prefix_backend == resolved_backend and upstream_model.strip():
            return upstream_model.strip()

    requested_backend = get_registry().resolve_backend_class(model) or model
    if requested_backend == resolved_backend:
        return default_embeddings_model_for_backend(resolved_backend)

    return model


async def _probe_models_for_backend(client: httpx.AsyncClient, backend_name: str, base_url: str, now: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    r = await client.get(f"{base_url.rstrip('/')}/models")
    r.raise_for_status()
    models = r.json().get("data", [])
    for m in models:
        mid = m.get("id")
        if mid:
            items.append({"id": f"{backend_name}:{mid}", "object": "model", "created": now, "owned_by": "local"})
    return items


@router.get("/v1/models")
async def list_models(req: Request):
    require_bearer(req)

    now = now_unix()
    data: Dict[str, Any] = {"object": "list", "data": []}

    async with httpx.AsyncClient(timeout=30) as client:
        for backend_name, cfg in llm_backends():
            try:
                data["data"].extend(await _probe_models_for_backend(client, backend_name, cfg.base_url, now))
            except Exception:
                pass

    data["data"].append({"id": "auto", "object": "model", "created": now, "owned_by": "gateway"})
    registry = get_registry()
    for provider_name in ("vllm", "mlx"):
        provider_backend = registry.get_backend(registry.resolve_backend_class(provider_name))
        if provider_backend is not None and (provider_backend.base_url or "").strip():
            data["data"].append({"id": provider_name, "object": "model", "created": now, "owned_by": "gateway"})
    for backend_name, _cfg in llm_backends():
        data["data"].append({"id": backend_name, "object": "model", "created": now, "owned_by": "gateway"})

    # Add configured aliases so clients can discover stable names.
    aliases = get_aliases()
    for alias_name in sorted(aliases.keys()):
        a = aliases[alias_name]
        item: Dict[str, Any] = {"id": alias_name, "object": "model", "created": now, "owned_by": "gateway"}
        # Extra fields are safe for most OpenAI-compatible clients and helpful for debugging.
        item["backend"] = a.backend
        item["upstream_model"] = a.upstream_model
        if a.context_window:
            item["context_window"] = a.context_window
        if a.tools is not None:
            item["tools"] = a.tools
        if a.max_tokens_cap is not None:
            item["max_tokens_cap"] = a.max_tokens_cap
        if a.temperature_cap is not None:
            item["temperature_cap"] = a.temperature_cap
        data["data"].append(item)

    return data


@router.get("/v1/models/{model_id}")
async def get_model(req: Request, model_id: str):
    require_bearer(req)
    return {"id": model_id, "object": "model", "created": now_unix(), "owned_by": "local"}


@router.post("/v1/chat/completions")
async def chat_completions(req: Request):
    require_bearer(req)
    body = await req.json()
    cc = ChatCompletionRequest(**body)
    cc.messages = await inject_memory(cc.messages, req=req)

    allowed_tools = None
    try:
        pol = getattr(req.state, "token_policy", None)
        if isinstance(pol, dict):
            allowed_tools = allowed_tool_names_for_policy(pol)
    except Exception:
        allowed_tools = None

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers=hdrs,
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=bool(cc.tools),
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )
    backend = route.backend
    model_name = route.model
    
    # Resolve to backend_class for capability gating and admission control
    registry = get_registry()
    backend_class = registry.resolve_backend_class(backend)
    
    # Check backend health/readiness
    check_backend_ready(backend_class, route_kind="chat")
    
    # Check capability
    await check_capability(backend_class, "chat")
    
    # Acquire admission slot
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")

    try:
        # Request instrumentation metadata (used by middleware JSONL logger).
        try:
            inst = getattr(req.state, "instrument", None)
            if not isinstance(inst, dict):
                inst = {}
            inst.update(
                {
                    "op": "chat.completions",
                    "backend": backend,
                    "backend_class": backend_class,
                    "upstream_model": model_name,
                    "router_reason": route.reason,
                    "has_tools": bool(cc.tools),
                }
            )
            req.state.instrument = inst
        except Exception:
            pass

        alias_name = _selected_alias_name(cc.model, route.reason)
        cc = _apply_alias_constraints(cc, alias_name=alias_name)

        logger.debug(
            "route chat.completions model=%r stream=%s tools=%s -> backend=%s upstream_model=%s reason=%s",
            cc.model,
            bool(cc.stream),
            bool(cc.tools),
            backend,
            model_name,
            route.reason,
        )

        if cc.stream and cc.tools:
            raise HTTPException(status_code=400, detail="stream=true not supported when tools are provided")

        if cc.stream:
            gen = stream_backend_chat_as_openai(cc, backend, model_name)
            out = StreamingResponse(gen, media_type="text/event-stream")
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            return out

        t0 = time.monotonic()
        if cc.tools:
            resp = await tool_loop(cc, backend, model_name, allowed_tools=allowed_tools)
        else:
            resp = await call_backend_chat(cc, backend, model_name)
        try:
            inst = getattr(req.state, "instrument", None)
            if isinstance(inst, dict):
                inst["upstream_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        except Exception:
            pass

        out = JSONResponse(resp)
        out.headers["X-Backend-Used"] = backend
        out.headers["X-Model-Used"] = model_name
        out.headers["X-Router-Reason"] = route.reason
        return out
    finally:
        # Release admission slot
        admission.release(backend_class, "chat")


@router.post("/v1/completions")
async def completions(req: Request):
    require_bearer(req)
    body = await req.json()
    cr = CompletionRequest(**body)

    if isinstance(cr.prompt, str):
        prompt_text = cr.prompt
    elif isinstance(cr.prompt, list) and all(isinstance(x, str) for x in cr.prompt):
        prompt_text = "\n".join(cr.prompt)
    else:
        raise HTTPException(status_code=400, detail="prompt must be a string or list of strings")

    cc = ChatCompletionRequest(
        model=cr.model,
        messages=[ChatMessage(role="user", content=prompt_text)],
        temperature=cr.temperature,
        max_tokens=cr.max_tokens,
        stream=bool(cr.stream),
    )

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers=hdrs,
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
    )
    backend = route.backend
    model_name = route.model

    # Apply caps/constraints based on the chosen alias (if any).
    alias_name = _selected_alias_name(cc.model, route.reason)
    cc = _apply_alias_constraints(cc, alias_name=alias_name)

    if cc.stream:
        stream_id = new_id("cmpl")
        created = now_unix()
        used_model_id = backend_model_id(backend, model_name)

        async def gen() -> AsyncIterator[bytes]:
            async for sse_bytes in stream_backend_chat_as_openai(cc, backend, model_name):
                for line in sse_bytes.splitlines():
                    if not line.startswith(b"data:"):
                        continue
                    data = line[len(b"data:") :].strip()
                    if data == b"[DONE]":
                        yield sse_done()
                        return
                    try:
                        j = json.loads(data)
                    except Exception:
                        continue
                    delta = (((j or {}).get("choices") or [{}])[0].get("delta") or {})
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield (
                            f"data: {json.dumps({'id': stream_id, 'object': 'text_completion', 'created': created, 'model': used_model_id, 'choices': [{'index': 0, 'text': text, 'finish_reason': None}]}, separators=(',', ':'))}\n\n"
                        ).encode("utf-8")

            yield (
                f"data: {json.dumps({'id': stream_id, 'object': 'text_completion', 'created': created, 'model': used_model_id, 'choices': [{'index': 0, 'text': '', 'finish_reason': 'stop'}]}, separators=(',', ':'))}\n\n"
            ).encode("utf-8")
            yield sse_done()

        out = StreamingResponse(gen(), media_type="text/event-stream")
        out.headers["X-Backend-Used"] = backend
        out.headers["X-Model-Used"] = model_name
        out.headers["X-Router-Reason"] = route.reason
        return out

    chat_resp = await call_backend_chat(cc, backend, model_name)

    msg = ((chat_resp.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content")
    if not isinstance(text, str):
        text = ""

    resp = {
        "id": new_id("cmpl"),
        "object": "text_completion",
        "created": now_unix(),
        "model": backend_model_id(backend, model_name),
        "choices": [{"index": 0, "text": text, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    out = JSONResponse(resp)
    out.headers["X-Backend-Used"] = backend
    out.headers["X-Model-Used"] = model_name
    out.headers["X-Router-Reason"] = route.reason
    return out


@router.post("/v1/rerank")
async def rerank(req: Request):
    require_bearer(req)
    body = await req.json()
    rr = RerankRequest(**body)

    if not rr.query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty")
    if not rr.documents:
        raise HTTPException(status_code=400, detail="documents must be non-empty")
    if any((not isinstance(d, str) or not d) for d in rr.documents):
        raise HTTPException(status_code=400, detail="documents must be a list of non-empty strings")

    top_n = rr.top_n if isinstance(rr.top_n, int) and rr.top_n > 0 else len(rr.documents)
    top_n = min(top_n, len(rr.documents))

    backend = S.EMBEDDINGS_BACKEND
    model_used = _normalize_embeddings_request_model(rr.model, backend)

    try:
        q_emb = (await embed_backend([rr.query], backend, model_used))[0]
        doc_embs = await embed_backend(rr.documents, backend, model_used)
    except httpx.HTTPStatusError as e:
        detail = {"upstream": backend, "status": e.response.status_code, "body": e.response.text[:5000]}
        logger.warning("/v1/rerank upstream HTTP error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)
    except httpx.RequestError as e:
        detail = {"upstream": backend, "error": str(e)}
        logger.warning("/v1/rerank upstream request error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)

    scored = []
    for i, emb in enumerate(doc_embs):
        s = memory_v2.cosine(q_emb, emb)
        scored.append((s, i))
    scored.sort(key=lambda x: x[0], reverse=True)

    data = []
    for rank, (score, i) in enumerate(scored[:top_n]):
        data.append({"index": i, "relevance_score": float(score), "document": rr.documents[i]})

    return {"object": "list", "data": data, "model": model_used}


@router.post("/v1/embeddings")
async def embeddings(req: Request):
    require_bearer(req)
    body = await req.json()
    er = EmbeddingsRequest(**body)

    if isinstance(er.input, str):
        texts = [er.input]
    elif isinstance(er.input, list) and all(isinstance(x, str) for x in er.input):
        texts = er.input
    else:
        raise HTTPException(status_code=400, detail="input must be a string or list of strings")

    backend = S.EMBEDDINGS_BACKEND
    model = _normalize_embeddings_request_model(er.model, backend)

    try:
        embs = await embed_backend(texts, backend, model)
    except httpx.HTTPStatusError as e:
        detail = {"upstream": backend, "status": e.response.status_code, "body": e.response.text[:5000]}
        logger.warning("/v1/embeddings upstream HTTP error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)
    except httpx.RequestError as e:
        detail = {"upstream": backend, "error": str(e)}
        logger.warning("/v1/embeddings upstream request error: %s", detail)
        raise HTTPException(status_code=502, detail=detail)

    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": embs[i]} for i in range(len(embs))],
        "model": model,
    }


@router.post("/v1/responses")
async def responses(req: Request):
    """Minimal OpenAI Responses API compatibility layer (non-stream).

    This maps a Responses-style request onto the existing chat completion path.
    """

    require_bearer(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="model must be a non-empty string")

    stream = bool(body.get("stream") or False)

    temperature = body.get("temperature")
    max_tokens = body.get("max_output_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_tokens")

    raw_input = body.get("input")
    messages: list[ChatMessage] = []
    if isinstance(raw_input, str):
        messages = [ChatMessage(role="user", content=raw_input)]
    elif isinstance(raw_input, list) and raw_input and all(isinstance(x, dict) for x in raw_input):
        # Best-effort: treat as chat-style messages.
        messages = [ChatMessage(**x) for x in raw_input]  # type: ignore[arg-type]
    elif raw_input is None:
        # Some clients send chat-style messages under `messages`.
        raw_messages = body.get("messages")
        if isinstance(raw_messages, list) and raw_messages and all(isinstance(x, dict) for x in raw_messages):
            messages = [ChatMessage(**x) for x in raw_messages]  # type: ignore[arg-type]
        else:
            raise HTTPException(status_code=400, detail="input is required")
    else:
        raise HTTPException(status_code=400, detail="input must be a string or list of message objects")

    tools = body.get("tools")

    cc = ChatCompletionRequest(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=body.get("tool_choice"),
        temperature=float(temperature) if temperature is not None else None,
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        stream=False,
    )
    cc.messages = await inject_memory(cc.messages, req=req)

    if stream and cc.tools:
        raise HTTPException(status_code=400, detail="stream=true not supported when tools are provided")

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers=hdrs,
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=bool(cc.tools),
        enable_policy=S.ROUTER_ENABLE_POLICY,
    )
    backend = route.backend
    model_name = route.model

    alias_name = _selected_alias_name(cc.model, route.reason)
    cc = _apply_alias_constraints(cc, alias_name=alias_name)

    if stream:
        response_id = new_id("resp")
        created = now_unix()
        used_model_id = backend_model_id(backend, model_name)

        upstream_gen = stream_backend_chat_as_openai(cc, backend, model_name)

        async def gen() -> AsyncIterator[bytes]:
            # Best-effort Responses API SSE.
            yield (
                f"data: {json.dumps({'type':'response.created','response':{'id':response_id,'object':'response','created':created,'model':used_model_id}}, separators=(',', ':'))}\n\n"
            ).encode("utf-8")

            async for chunk in upstream_gen:
                for line in chunk.splitlines():
                    if not line.startswith(b"data:"):
                        continue
                    data = line[len(b"data:") :].strip()
                    if data == b"[DONE]":
                        yield (
                            f"data: {json.dumps({'type':'response.completed','response':{'id':response_id}}, separators=(',', ':'))}\n\n"
                        ).encode("utf-8")
                        yield sse_done()
                        return
                    try:
                        j = json.loads(data)
                    except Exception:
                        continue
                    delta = (((j or {}).get("choices") or [{}])[0].get("delta") or {})
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield (
                            f"data: {json.dumps({'type':'response.output_text.delta','delta':text}, separators=(',', ':'))}\n\n"
                        ).encode("utf-8")

            yield (
                f"data: {json.dumps({'type':'response.completed','response':{'id':response_id}}, separators=(',', ':'))}\n\n"
            ).encode("utf-8")
            yield sse_done()

        out = StreamingResponse(gen(), media_type="text/event-stream")
        out.headers["X-Backend-Used"] = backend
        out.headers["X-Model-Used"] = model_name
        out.headers["X-Router-Reason"] = route.reason
        return out

    if cc.tools:
        allowed_tools = None
        try:
            pol = getattr(req.state, "token_policy", None)
            if isinstance(pol, dict):
                allowed_tools = allowed_tool_names_for_policy(pol)
        except Exception:
            allowed_tools = None
        chat_resp = await tool_loop(cc, backend, model_name, allowed_tools=allowed_tools)
    else:
        chat_resp = await call_backend_chat(cc, backend, model_name)

    msg = ((chat_resp.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content")
    if not isinstance(text, str):
        text = ""

    out = {
        "id": new_id("resp"),
        "object": "response",
        "created": now_unix(),
        "model": backend_model_id(backend, model_name),
        "output": [
            {
                "type": "message",
                "id": new_id("msg"),
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": chat_resp.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    resp = JSONResponse(out)
    resp.headers["X-Backend-Used"] = backend
    resp.headers["X-Model-Used"] = model_name
    resp.headers["X-Router-Reason"] = route.reason
    return resp
