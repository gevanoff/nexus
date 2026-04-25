from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List

import httpx
from fastapi import HTTPException

from app.backends import backend_provider_name, get_registry
from app.config import S
from app.httpx_client import httpx_client as _httpx_client
from app.model_aliases import get_alias
from app.models import ChatCompletionRequest
from app.openai_utils import sanitize_chat_choices, sse, sse_done
from app.streaming import passthrough_sse


def _normalize_messages_for_openai_backend(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    last_role: str | None = None
    for m in msgs:
        role = (m.get("role") or "").strip()

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


def _default_base_url_for_provider(provider: str) -> str:
    name = (provider or "").strip().lower()
    if name == "vllm":
        return (S.VLLM_BASE_URL or "").rstrip("/")
    if name == "mlx":
        return (S.MLX_BASE_URL or "").rstrip("/")
    return ""


def backend_model_id(backend_name: str, model_name: str) -> str:
    resolved, _provider, _base_url = _resolve_backend_target(backend_name)
    return f"{resolved}:{model_name}"


def default_embeddings_model_for_backend(backend_name: str) -> str:
    _resolved, provider, _base_url = _resolve_backend_target(backend_name)
    configured = (S.EMBEDDINGS_MODEL or "").strip()

    if configured and configured.lower() not in {"default", "auto"}:
        if configured != "nomic-embed-text":
            return configured

    alias = get_alias("embeddings")
    if alias and (alias.upstream_model or "").strip():
        return alias.upstream_model

    if provider == "vllm":
        return S.VLLM_MODEL_EMBEDDINGS

    return "mlx-community/bge-small-en-v1.5-8bit"


def route_request_for_backend(req: ChatCompletionRequest, backend_name: str, model_name: str) -> ChatCompletionRequest:
    _resolved, provider, _base_url = _resolve_backend_target(backend_name)
    if provider not in {"mlx", "vllm"}:
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


async def call_openai_chat(
    req: ChatCompletionRequest,
    *,
    base_url: str | None = None,
    backend_name: str = "local_vllm",
) -> Dict[str, Any]:
    payload = req.model_dump(exclude_none=True)
    if "messages" in payload and isinstance(payload["messages"], list):
        payload["messages"] = _normalize_messages_for_openai_backend(payload["messages"])

    provider = backend_provider_name(backend_name)
    target = (base_url or _default_base_url_for_provider(provider)).rstrip("/")

    async with _httpx_client(timeout=600) as client:
        try:
            r = await client.post(f"{target}/chat/completions", json=payload)
            r.raise_for_status()
            out = r.json()
            if isinstance(out, dict):
                sanitize_chat_choices(out)
                out["model"] = backend_model_id(backend_name, req.model)
            return out
        except httpx.HTTPStatusError as e:
            detail = {"upstream": backend_name, "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": backend_name, "error": str(e)})


async def embed_openai_backend(
    texts: List[str],
    model: str,
    *,
    base_url: str | None = None,
    backend_name: str = "local_vllm_embeddings",
) -> List[List[float]]:
    provider = backend_provider_name(backend_name)
    target = (base_url or _default_base_url_for_provider(provider)).rstrip("/")
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
    resolved, _provider, base_url = _resolve_backend_target(backend_name)
    return await embed_openai_backend(texts, model, base_url=base_url, backend_name=resolved)


async def embed_text_for_memory(text: str) -> list[float]:
    backend = (S.EMBEDDINGS_BACKEND or S.DEFAULT_BACKEND or "local_mlx").strip()
    model = default_embeddings_model_for_backend(backend)
    return (await embed_backend([text], backend, model))[0]


async def transcribe_openai_audio(
    *,
    backend_name: str,
    file_name: str,
    file_bytes: bytes,
    content_type: str,
    form_fields: Dict[str, Any] | None = None,
) -> tuple[str, Any, str]:
    resolved, _provider, base_url = _resolve_backend_target(backend_name)
    timeout = float(getattr(S, "TRANSCRIPTION_TIMEOUT_SEC", 600.0) or 600.0)
    data: Dict[str, Any] = {}
    if isinstance(form_fields, dict):
        for k, v in form_fields.items():
            if v is None:
                continue
            data[str(k)] = v

    async with _httpx_client(timeout=timeout) as client:
        try:
            r = await client.post(
                f"{base_url}/audio/transcriptions",
                data=data,
                files={"file": (file_name, file_bytes, content_type or "application/octet-stream")},
            )
            r.raise_for_status()
            response_type = (r.headers.get("content-type") or "").lower()
            if "json" in response_type:
                return "json", r.json(), response_type
            return "text", r.text, response_type or "text/plain; charset=utf-8"
        except httpx.HTTPStatusError as e:
            detail = {"upstream": resolved, "status": e.response.status_code, "body": e.response.text[:5000]}
            raise HTTPException(status_code=502, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail={"upstream": resolved, "error": str(e)})


async def stream_openai_chat(
    payload: Dict[str, Any],
    *,
    base_url: str | None = None,
    backend_name: str = "local_vllm",
) -> AsyncIterator[bytes]:
    if "messages" in payload and isinstance(payload["messages"], list):
        payload = dict(payload)
        payload["messages"] = _normalize_messages_for_openai_backend(payload["messages"])

    provider = backend_provider_name(backend_name)
    target = (base_url or _default_base_url_for_provider(provider)).rstrip("/")

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


async def call_backend_chat(req: ChatCompletionRequest, backend_name: str, model_name: str) -> Dict[str, Any]:
    resolved, _provider, base_url = _resolve_backend_target(backend_name)
    routed_req = route_request_for_backend(req, resolved, model_name)
    return await call_openai_chat(routed_req, base_url=base_url, backend_name=resolved)


def stream_backend_chat_as_openai(req: ChatCompletionRequest, backend_name: str, model_name: str) -> AsyncIterator[bytes]:
    resolved, _provider, base_url = _resolve_backend_target(backend_name)
    routed_req = route_request_for_backend(req, resolved, model_name)
    payload = routed_req.model_dump(exclude_none=True)
    payload["model"] = model_name
    payload["stream"] = True
    return stream_openai_chat(payload, base_url=base_url, backend_name=resolved)
