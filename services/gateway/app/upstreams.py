from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List

import httpx
from fastapi import HTTPException

from app.backends import backend_provider_name, get_registry
from app.config import S, logger
from app.httpx_client import httpx_client as _httpx_client
from app.model_aliases import get_alias
from app.models import ChatCompletionRequest
from app.openai_utils import new_id, now_unix, sse, sse_done
from app.streaming import ollama_ndjson_to_openai_sse, passthrough_sse


def _normalize_messages_for_mlx(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    last_role: str | None = None
    for m in msgs:
        role = (m.get("role") or "").strip()
        if role == "system":
            role = "user"

        content = m.get("content")
        if content is None:
            normalized_content: Any = ""
        elif isinstance(content, str):
            normalized_content = content
        elif isinstance(content, (list, dict)):
            normalized_content = content
        else:
            try:
                normalized_content = json.dumps(content, ensure_ascii=False)
            except Exception:
                normalized_content = str(content)

        if last_role is not None and last_role == role and out:
            prev = out[-1]
            prev_content = prev.get("content") or ""
            if isinstance(prev_content, str) and isinstance(normalized_content, str):
                prev["content"] = prev_content + "\n" + normalized_content
            else:
                out.append({"role": role, "content": normalized_content})
                last_role = role
        else:
            out.append({"role": role, "content": normalized_content})
            last_role = role
    return out


def _resolve_backend_target(backend_name: str) -> tuple[str, str, str]:
    registry = get_registry()
    resolved = registry.resolve_backend_class(backend_name)
    config = registry.get_backend(resolved)
    if config is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "backend_not_found", "backend_class": backend_name, "message": f"Backend {backend_name} is not configured"},
        )
    base_url = (config.base_url or "").rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail={"error": "backend_not_ready", "backend_class": resolved, "message": f"Backend {resolved} has no base_url configured"},
        )
    return resolved, backend_provider_name(resolved), base_url


def backend_model_id(backend_name: str, model_name: str) -> str:
    resolved, _provider, _base_url = _resolve_backend_target(backend_name)
    return f"{resolved}:{model_name}"


def default_embeddings_model_for_backend(backend_name: str) -> str:
    configured = (S.EMBEDDINGS_MODEL or "").strip()
    provider = backend_provider_name(backend_name)

    if configured and configured.lower() not in {"default", "auto"}:
        if not (provider == "mlx" and configured == "nomic-embed-text"):
            return configured

    if provider == "ollama":
        return configured or "nomic-embed-text"

    for alias_name in ("default", "fast"):
        alias = get_alias(alias_name)
        if not alias or not (alias.upstream_model or "").strip():
            continue
        if backend_provider_name(alias.backend) == provider:
            return alias.upstream_model

    return (S.MLX_MODEL_STRONG or S.MLX_MODEL_DEFAULT or "mlx-community/gemma-2-2b-it-8bit").strip()


def route_request_for_backend(req: ChatCompletionRequest, backend_name: str, model_name: str) -> ChatCompletionRequest:
    _resolved, provider, _base_url = _resolve_backend_target(backend_name)
    if provider != "mlx":
        return req
    return ChatCompletionRequest(
        model=model_name,
        messages=req.messages,
        tools=req.tools,
        tool_choice=req.tool_choice,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        stream=req.stream,
    )


async def call_mlx_openai(req: ChatCompletionRequest, *, base_url: str | None = None, backend_name: str = "mlx") -> Dict[str, Any]:
    payload = req.model_dump(exclude_none=True)
    if "messages" in payload and isinstance(payload["messages"], list):
        payload["messages"] = _normalize_messages_for_mlx(payload["messages"])

    target = (base_url or S.MLX_BASE_URL).rstrip("/")

    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(f"{target}/chat/completions", json=payload)
            r.raise_for_status()
            out = r.json()
            if isinstance(out, dict):
                out["model"] = backend_model_id(backend_name, req.model)
            return out
        except httpx.HTTPStatusError as e:
            detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": str(e)})


async def call_ollama(
    req: ChatCompletionRequest,
    model_name: str,
    *,
    base_url: str | None = None,
    backend_name: str = "ollama",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": [m.model_dump(exclude_none=True) for m in req.messages],
        "stream": False,
    }
    if req.tools:
        payload["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
    if req.temperature is not None:
        payload.setdefault("options", {})["temperature"] = req.temperature

    target = (base_url or S.OLLAMA_BASE_URL).rstrip("/")

    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(f"{target}/api/chat", json=payload)
            r.raise_for_status()
            out = r.json()
            if isinstance(out, dict) and isinstance(out.get("error"), str) and out.get("error"):
                raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": out.get("error")})
        except httpx.HTTPStatusError as e:
            detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": str(e)})

    msg = out.get("message", {})
    return {
        "id": new_id("chatcmpl"),
        "object": "chat.completion",
        "created": now_unix(),
        "choices": [{"index": 0, "message": msg, "finish_reason": out.get("done_reason", "stop")}],
        "model": backend_model_id(backend_name, model_name),
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def embed_ollama(
    texts: List[str],
    model: str,
    *,
    base_url: str | None = None,
    backend_name: str = "ollama",
) -> List[List[float]]:
    target = (base_url or S.OLLAMA_BASE_URL).rstrip("/")
    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(
                f"{target}/api/embed",
                json={"model": model, "input": texts},
            )
            r.raise_for_status()
            j = r.json()
            embs = j.get("embeddings")
            if isinstance(embs, list) and embs and isinstance(embs[0], list):
                return embs
        except Exception:
            pass

        out: List[List[float]] = []
        for t in texts:
            r = await client.post(
                f"{target}/api/embeddings",
                json={"model": model, "prompt": t},
            )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
                raise HTTPException(status_code=502, detail=detail)
            j = r.json()
            e = j.get("embedding")
            if not isinstance(e, list):
                raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": "No embedding in response"})
            out.append(e)
        return out


async def embed_mlx(
    texts: List[str],
    model: str,
    *,
    base_url: str | None = None,
    backend_name: str = "mlx",
) -> List[List[float]]:
    target = (base_url or S.MLX_BASE_URL).rstrip("/")
    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(
                f"{target}/embeddings",
                json={"model": model, "input": texts if len(texts) > 1 else texts[0]},
            )
            r.raise_for_status()
            j = r.json()
            data = j.get("data", [])
            out: List[List[float]] = []
            for item in data:
                emb = (item or {}).get("embedding")
                if isinstance(emb, list):
                    out.append(emb)
            if len(out) != len(texts):
                raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": "Unexpected embeddings shape"})
            return out
        except httpx.HTTPStatusError as e:
            detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": str(e)})


async def embed_backend(texts: List[str], backend_name: str, model: str) -> List[List[float]]:
    resolved, provider, base_url = _resolve_backend_target(backend_name)
    if provider == "ollama":
        return await embed_ollama(texts, model, base_url=base_url, backend_name=resolved)
    return await embed_mlx(texts, model, base_url=base_url, backend_name=resolved)


async def embed_text_for_memory(text: str) -> list[float]:
    backend = (S.EMBEDDINGS_BACKEND or S.DEFAULT_BACKEND or "local_mlx").strip()
    model = default_embeddings_model_for_backend(backend)
    return (await embed_backend([text], backend, model))[0]


async def stream_mlx_openai_chat(
    payload: Dict[str, Any],
    *,
    base_url: str | None = None,
    backend_name: str = "mlx",
) -> AsyncIterator[bytes]:
    if "messages" in payload and isinstance(payload["messages"], list):
        payload = dict(payload)
        payload["messages"] = _normalize_messages_for_mlx(payload["messages"])

    target = (base_url or S.MLX_BASE_URL).rstrip("/")

    async with _httpx_client(timeout=None) as client:
        try:
            async with client.stream(
                "POST",
                f"{target}/chat/completions",
                json=payload,
                headers={"accept": "text/event-stream"},
            ) as r:
                r.raise_for_status()
                async for chunk in passthrough_sse(r):
                    yield chunk
        except httpx.HTTPStatusError as e:
            detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()
        except httpx.RequestError as e:
            detail = {"upstream": backend_name, "error": str(e)}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()


async def stream_ollama_chat_as_openai(
    req: ChatCompletionRequest,
    model_name: str,
    *,
    base_url: str | None = None,
    model_id: str | None = None,
    backend_name: str = "ollama",
) -> AsyncIterator[bytes]:
    chunk_id = new_id("chatcmpl")
    created = now_unix()
    resolved_model_id = model_id or backend_model_id(backend_name, model_name)
    finish_sent = False

    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": resolved_model_id,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    target = (base_url or S.OLLAMA_BASE_URL).rstrip("/")

    try:
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            "stream": True,
        }
        if req.tools:
            payload["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
        if req.temperature is not None:
            payload.setdefault("options", {})["temperature"] = req.temperature

        async with _httpx_client(timeout=None) as client:
            async with client.stream("POST", f"{target}/api/chat", json=payload) as r:
                r.raise_for_status()
                async for chunk in ollama_ndjson_to_openai_sse(
                    r,
                    model_name=resolved_model_id,
                    chunk_id=chunk_id,
                    created=created,
                    emit_role_chunk=False,
                ):
                    if chunk == sse_done():
                        break
                    if b'"finish_reason":"' in chunk:
                        finish_sent = True
                    yield chunk

    except asyncio.CancelledError:
        raise
    except httpx.HTTPStatusError as e:
        detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
        yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
    except httpx.RequestError as e:
        detail = {"upstream": backend_name, "error": str(e)}
        yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
    except Exception as e:
        logger.exception("Unexpected error in Ollama streaming")
        detail = {"upstream": backend_name, "error": str(e)}
        yield sse({"error": {"message": "Gateway streaming error", "type": "internal_error", "param": None, "code": None, "detail": detail}})
    finally:
        if not finish_sent:
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
        yield sse_done()


async def call_backend_chat(req: ChatCompletionRequest, backend_name: str, model_name: str) -> Dict[str, Any]:
    resolved, provider, base_url = _resolve_backend_target(backend_name)
    routed_req = route_request_for_backend(req, resolved, model_name)
    if provider == "ollama":
        return await call_ollama(req, model_name, base_url=base_url, backend_name=resolved)
    return await call_mlx_openai(routed_req, base_url=base_url, backend_name=resolved)


def stream_backend_chat_as_openai(req: ChatCompletionRequest, backend_name: str, model_name: str) -> AsyncIterator[bytes]:
    resolved, provider, base_url = _resolve_backend_target(backend_name)
    if provider == "ollama":
        return stream_ollama_chat_as_openai(
            req,
            model_name,
            base_url=base_url,
            model_id=backend_model_id(resolved, model_name),
            backend_name=resolved,
        )
    routed_req = route_request_for_backend(req, resolved, model_name)
    payload = routed_req.model_dump(exclude_none=True)
    payload["model"] = model_name
    payload["stream"] = True
    return stream_mlx_openai_chat(payload, base_url=base_url, backend_name=resolved)
