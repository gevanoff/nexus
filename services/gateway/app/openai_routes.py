from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.auth import require_bearer
from app.config import S, logger
from app.backends import (
    backend_supports_tool_calling,
    check_capability,
    get_admission_controller,
    get_registry,
    llm_backends,
)
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

    temperature = cc.temperature
    if temperature is not None and a.temperature_cap is not None:
        temperature = min(float(temperature), float(a.temperature_cap))

    max_tokens = cc.max_tokens
    if max_tokens is not None and a.max_tokens_cap is not None:
        max_tokens = min(int(max_tokens), int(a.max_tokens_cap))

    if temperature == cc.temperature and max_tokens == cc.max_tokens:
        return cc

    return cc.model_copy(
        update={
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    )


def _openai_error_payload(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    param: Optional[str] = None,
    code: Optional[str] = "invalid_request",
    detail: Any = None,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {
        "message": message,
        "type": error_type,
        "param": param,
        "code": code,
    }
    if detail is not None:
        error["detail"] = detail
    return {"error": error}


def _openai_error_response(
    message: str,
    *,
    status_code: int = 400,
    error_type: str = "invalid_request_error",
    param: Optional[str] = None,
    code: Optional[str] = "invalid_request",
    detail: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_openai_error_payload(
            message,
            error_type=error_type,
            param=param,
            code=code,
            detail=detail,
        ),
    )


def _validation_error_param(exc: ValidationError) -> Optional[str]:
    try:
        first = exc.errors(include_url=False)[0]
    except Exception:
        return None
    loc = first.get("loc") if isinstance(first, dict) else None
    if not isinstance(loc, (list, tuple)):
        return None
    parts = [str(item) for item in loc if item not in {"body", None, ""}]
    return ".".join(parts) if parts else None


def _validation_error_message(exc: ValidationError) -> str:
    try:
        first = exc.errors(include_url=False)[0]
    except Exception:
        return str(exc)
    if not isinstance(first, dict):
        return str(exc)
    param = _validation_error_param(exc)
    msg = str(first.get("msg") or "request validation failed")
    if param:
        return f"Unsupported field or invalid request shape: {param}: {msg}"
    return f"Unsupported field or invalid request shape: {msg}"


def _alias_allows_tools(alias_name: Optional[str]) -> bool:
    if not alias_name:
        return True
    alias = get_aliases().get(alias_name)
    if alias is None:
        return True
    return alias.tools is not False


def _tool_fields_present(cc: ChatCompletionRequest) -> bool:
    return bool(cc.tools) or cc.tool_choice is not None or cc.parallel_tool_calls is not None


def _body_keys(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    return sorted(str(key) for key in body.keys())


def _request_messages_summary(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        item = {
            "role": message.role,
            "has_content": message.content is not None,
            "has_tool_calls": bool(message.tool_calls),
            "has_tool_call_id": message.tool_call_id is not None,
        }
        if message.name is not None:
            item["name"] = message.name
        out.append(item)
    return out


def _tool_fields_action(before: ChatCompletionRequest, after: ChatCompletionRequest, *, request_shape_action: str) -> str:
    if request_shape_action == "shimmed" and _tool_fields_present(before):
        if _tool_fields_present(after):
            return "shimmed"
        return "stripped"
    if not _tool_fields_present(before):
        return "absent"
    if (
        before.tools == after.tools
        and before.tool_choice == after.tool_choice
        and before.parallel_tool_calls == after.parallel_tool_calls
    ):
        return "passed_through"
    return "stripped"


def _log_openai_request(
    *,
    endpoint: str,
    body: Any,
    cc: ChatCompletionRequest,
    alias_name: Optional[str],
    route: Any,
    tool_fields_action: str,
    request_shape_action: str,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "request_keys": _body_keys(body),
        "selected_model_alias": alias_name,
        "model": cc.model,
        "stream": bool(cc.stream),
        "has_tools": bool(cc.tools),
        "tool_choice": cc.tool_choice,
        "parallel_tool_calls": cc.parallel_tool_calls,
        "tool_fields_action": tool_fields_action,
        "request_shape_action": request_shape_action,
        "message_count": len(cc.messages),
        "message_summary": _request_messages_summary(cc.messages),
        "route_backend": getattr(route, "backend", None),
        "route_model": getattr(route, "model", None),
        "route_reason": getattr(route, "reason", None),
    }
    if bool(getattr(S, "OPENAI_DEBUG_LOG_MESSAGE_CONTENT", False)):
        payload["messages"] = [message.model_dump(exclude_none=True) for message in cc.messages]

    logger.debug("openai compatibility request %s", json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))
    if tool_fields_action != "absent":
        logger.info(
            "openai compatibility endpoint=%s alias=%s stream=%s tools=%s tool_choice=%r tool_fields=%s route_backend=%s route_model=%s",
            endpoint,
            alias_name,
            bool(cc.stream),
            bool(cc.tools),
            cc.tool_choice,
            tool_fields_action,
            getattr(route, "backend", None),
            getattr(route, "model", None),
        )


def _tool_choice_requires_tools(tool_choice: Any) -> bool:
    if tool_choice is None:
        return False
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        return normalized not in {"", "auto", "none"}
    if isinstance(tool_choice, dict):
        normalized = str(tool_choice.get("type") or "").strip().lower()
        return normalized not in {"", "auto", "none"}
    return True


def _degradation_reason(*, alias_name: Optional[str], backend_class: str, backend_supports_tools: bool) -> Optional[str]:
    reasons: list[str] = []
    if alias_name and not _alias_allows_tools(alias_name):
        reasons.append(f"model alias '{alias_name}' disables tools")
    if not backend_supports_tools:
        reasons.append(f"backend '{backend_class}' does not support native tool calling")
    if not reasons:
        return None
    return "; ".join(reasons)


def _normalize_chat_request_for_backend(
    cc: ChatCompletionRequest,
    *,
    alias_name: Optional[str],
    backend_class: str,
) -> tuple[ChatCompletionRequest, Optional[str], bool]:
    if not _tool_fields_present(cc):
        return cc, None, False

    backend_supports_tools = backend_supports_tool_calling(backend_class)
    degradation_reason = _degradation_reason(
        alias_name=alias_name,
        backend_class=backend_class,
        backend_supports_tools=backend_supports_tools,
    )
    if degradation_reason is None:
        return cc, None, False

    if _tool_choice_requires_tools(cc.tool_choice):
        return cc, degradation_reason, True

    logger.info(
        "chat.completions degrading tool fields alias=%s backend=%s tool_choice=%r parallel_tool_calls=%r reason=%s",
        alias_name or "-",
        backend_class,
        cc.tool_choice,
        cc.parallel_tool_calls,
        degradation_reason,
    )
    return (
        cc.model_copy(update={"tools": None, "tool_choice": None, "parallel_tool_calls": None}),
        degradation_reason,
        False,
    )


def _route_chat_request(
    cc: ChatCompletionRequest,
    *,
    headers: Dict[str, str],
    enable_request_type: bool = False,
) -> tuple[Any, str, Optional[str]]:
    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers=headers,
        # Preserve requested alias/backend selection and degrade unsupported tool fields later.
        # This avoids compatibility failures caused by tool-shaped requests being rerouted to
        # a different backend solely because tool fields were present.
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=enable_request_type,
    )
    backend_class = get_registry().resolve_backend_class(route.backend)
    alias_name = _selected_alias_name(cc.model, route.reason)
    return route, backend_class, alias_name


def _chat_completion_request_from_response_body(body: dict[str, Any], messages: list[ChatMessage], *, stream: bool) -> ChatCompletionRequest:
    payload = dict(body)
    payload.pop("input", None)
    payload.pop("max_output_tokens", None)
    payload["messages"] = messages
    payload["max_tokens"] = body.get("max_output_tokens") if body.get("max_output_tokens") is not None else body.get("max_tokens")
    payload["stream"] = bool(stream)
    return ChatCompletionRequest(**payload)


def _response_output_from_chat_message(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, str):
        output.append(
            {
                "type": "message",
                "id": new_id("msg"),
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            output.append(
                {
                    "type": "function_call",
                    "id": new_id("fc"),
                    "call_id": tool_call.get("id"),
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "",
                }
            )

    if output:
        return output

    return [
        {
            "type": "message",
            "id": new_id("msg"),
            "role": "assistant",
            "content": [{"type": "output_text", "text": ""}],
        }
    ]


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
    seen_ids: set[str] = set()

    def add_model_item(item: Dict[str, Any]) -> None:
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen_ids:
            return
        seen_ids.add(model_id)
        data["data"].append(item)

    async with httpx.AsyncClient(timeout=30) as client:
        for backend_name, cfg in llm_backends():
            try:
                for item in await _probe_models_for_backend(client, backend_name, cfg.base_url, now):
                    add_model_item(item)
            except Exception:
                pass

    add_model_item({"id": "auto", "object": "model", "created": now, "owned_by": "gateway"})
    registry = get_registry()
    for provider_name in ("vllm", "vllm_fast", "mlx"):
        provider_backend = registry.get_backend(registry.resolve_backend_class(provider_name))
        if provider_backend is not None and (provider_backend.base_url or "").strip():
            add_model_item({"id": provider_name, "object": "model", "created": now, "owned_by": "gateway"})

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
        add_model_item(item)

    return data


@router.get("/v1/models/{model_id}")
async def get_model(req: Request, model_id: str):
    require_bearer(req)
    return {"id": model_id, "object": "model", "created": now_unix(), "owned_by": "local"}


@router.post("/v1/chat/completions")
async def chat_completions(req: Request):
    require_bearer(req)
    try:
        body = await req.json()
    except Exception as exc:
        return _openai_error_response(
            f"Unsupported field or invalid request shape: invalid JSON body ({type(exc).__name__})"
        )

    try:
        cc = ChatCompletionRequest(**body)
    except ValidationError as exc:
        return _openai_error_response(
            _validation_error_message(exc),
            param=_validation_error_param(exc),
            detail=exc.errors(include_url=False),
        )

    cc.messages = await inject_memory(cc.messages, req=req)

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route, backend_class, alias_name = _route_chat_request(
        cc,
        headers=hdrs,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )
    backend = route.backend
    model_name = route.model
    
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
                    "request_keys": _body_keys(body),
                    "selected_alias": alias_name,
                    "stream": bool(cc.stream),
                    "tool_choice": cc.tool_choice,
                    "tool_fields_action": tool_fields_action,
                }
            )
            req.state.instrument = inst
        except Exception:
            pass

        cc = _apply_alias_constraints(cc, alias_name=alias_name)
        original_cc = cc

        cc, degradation_reason, tools_required_error = _normalize_chat_request_for_backend(
            cc,
            alias_name=alias_name,
            backend_class=backend_class,
        )
        tool_fields_action = _tool_fields_action(original_cc, cc, request_shape_action="direct")
        _log_openai_request(
            endpoint="/v1/chat/completions",
            body=body,
            cc=cc,
            alias_name=alias_name,
            route=route,
            tool_fields_action=tool_fields_action,
            request_shape_action="direct",
        )
        if tools_required_error:
            return _openai_error_response(
                f"Unsupported field or invalid request shape: tools were explicitly required, but {degradation_reason}",
                param="tool_choice",
                detail={
                    "backend": backend_class,
                    "alias": alias_name,
                    "tool_choice": cc.tool_choice,
                },
            )

        logger.debug(
            "route chat.completions model=%r alias=%r stream=%s tools=%s tool_choice=%r tool_fields_action=%s degraded_tools=%s -> backend=%s upstream_model=%s reason=%s",
            cc.model,
            alias_name,
            bool(cc.stream),
            bool(cc.tools),
            cc.tool_choice,
            tool_fields_action,
            bool(degradation_reason),
            backend,
            model_name,
            route.reason,
        )

        if cc.stream:
            gen = stream_backend_chat_as_openai(cc, backend, model_name)
            out = StreamingResponse(gen, media_type="text/event-stream")
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            return out

        t0 = time.monotonic()
        resp = await call_backend_chat(cc, backend, model_name)
        try:
            inst = getattr(req.state, "instrument", None)
            if isinstance(inst, dict):
                inst["upstream_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                if degradation_reason:
                    inst["tool_degradation_reason"] = degradation_reason
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
    try:
        body = await req.json()
    except Exception as exc:
        return _openai_error_response(
            f"Unsupported field or invalid request shape: invalid JSON body ({type(exc).__name__})"
        )
    if not isinstance(body, dict):
        return _openai_error_response("Unsupported field or invalid request shape: body must be an object")

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return _openai_error_response(
            "Unsupported field or invalid request shape: model must be a non-empty string",
            param="model",
        )

    stream = bool(body.get("stream") or False)

    raw_input = body.get("input")
    messages: list[ChatMessage] = []
    try:
        if isinstance(raw_input, str):
            messages = [ChatMessage(role="user", content=raw_input)]
        elif isinstance(raw_input, list) and raw_input and all(isinstance(x, dict) for x in raw_input):
            messages = [ChatMessage(**x) for x in raw_input]  # type: ignore[arg-type]
        elif raw_input is None:
            raw_messages = body.get("messages")
            if isinstance(raw_messages, list) and raw_messages and all(isinstance(x, dict) for x in raw_messages):
                messages = [ChatMessage(**x) for x in raw_messages]  # type: ignore[arg-type]
            else:
                return _openai_error_response(
                    "Unsupported field or invalid request shape: input is required",
                    param="input",
                )
        else:
            return _openai_error_response(
                "Unsupported field or invalid request shape: input must be a string or list of message objects",
                param="input",
            )
        cc = _chat_completion_request_from_response_body(body, messages, stream=stream)
    except ValidationError as exc:
        return _openai_error_response(
            _validation_error_message(exc),
            param=_validation_error_param(exc),
            detail=exc.errors(include_url=False),
        )

    cc.messages = await inject_memory(cc.messages, req=req)

    hdrs = {k.lower(): v for k, v in req.headers.items()}
    route, backend_class, alias_name = _route_chat_request(cc, headers=hdrs)
    backend = route.backend
    model_name = route.model

    check_backend_ready(backend_class, route_kind="chat")
    await check_capability(backend_class, "chat")
    admission = get_admission_controller()
    await admission.acquire(backend_class, "chat")

    try:
        cc = _apply_alias_constraints(cc, alias_name=alias_name)
        original_cc = cc
        cc, degradation_reason, tools_required_error = _normalize_chat_request_for_backend(
            cc,
            alias_name=alias_name,
            backend_class=backend_class,
        )
        tool_fields_action = _tool_fields_action(original_cc, cc, request_shape_action="shimmed")
        _log_openai_request(
            endpoint="/v1/responses",
            body=body,
            cc=cc,
            alias_name=alias_name,
            route=route,
            tool_fields_action=tool_fields_action,
            request_shape_action="shimmed",
        )
        if tools_required_error:
            return _openai_error_response(
                f"Unsupported field or invalid request shape: tools were explicitly required, but {degradation_reason}",
                param="tool_choice",
                detail={
                    "backend": backend_class,
                    "alias": alias_name,
                    "tool_choice": cc.tool_choice,
                },
            )

        try:
            inst = getattr(req.state, "instrument", None)
            if not isinstance(inst, dict):
                inst = {}
            inst.update(
                {
                    "op": "responses",
                    "backend": backend,
                    "backend_class": backend_class,
                    "upstream_model": model_name,
                    "router_reason": route.reason,
                    "has_tools": bool(cc.tools),
                    "request_keys": _body_keys(body),
                    "selected_alias": alias_name,
                    "stream": bool(cc.stream),
                    "tool_choice": cc.tool_choice,
                    "tool_fields_action": tool_fields_action,
                }
            )
            if degradation_reason:
                inst["tool_degradation_reason"] = degradation_reason
            req.state.instrument = inst
        except Exception:
            pass

        logger.debug(
            "route responses model=%r alias=%r stream=%s tools=%s tool_choice=%r tool_fields_action=%s degraded_tools=%s -> backend=%s upstream_model=%s reason=%s",
            cc.model,
            alias_name,
            bool(cc.stream),
            bool(cc.tools),
            cc.tool_choice,
            tool_fields_action,
            bool(degradation_reason),
            backend,
            model_name,
            route.reason,
        )

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

        chat_resp = await call_backend_chat(cc, backend, model_name)

        msg = ((chat_resp.get("choices") or [{}])[0].get("message") or {})

        out = {
            "id": new_id("resp"),
            "object": "response",
            "created": now_unix(),
            "model": backend_model_id(backend, model_name),
            "output": _response_output_from_chat_message(msg),
            "usage": chat_resp.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

        resp = JSONResponse(out)
        resp.headers["X-Backend-Used"] = backend
        resp.headers["X-Model-Used"] = model_name
        resp.headers["X-Router-Reason"] = route.reason
        return resp
    finally:
        admission.release(backend_class, "chat")
