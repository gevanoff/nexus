from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List

import httpx
from fastapi import HTTPException

from app.backends import backend_provider_name, get_registry
from app.config import S, logger
from app.httpx_client import httpx_client as _httpx_client
from app.model_aliases import get_alias, get_aliases
from app.models import ChatCompletionRequest
from app.openai_utils import sanitize_chat_choices, sse, sse_done
from app.streaming import passthrough_sse


def _normalize_text_content_parts(content: list[Any]) -> str | None:
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"text", "input_text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
            continue
        return None
    return "\n".join(part for part in parts if part)


def _normalize_content_for_openai_backend(content: Any) -> Any:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _normalize_text_content_parts(content)
        if text is not None:
            return text
        return content
    if isinstance(content, dict):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _normalize_messages_for_openai_backend(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    last_role: str | None = None
    for m in msgs:
        role = (m.get("role") or "").strip()
        normalized_content = _normalize_content_for_openai_backend(m.get("content"))

        normalized_message: Dict[str, Any] = {"role": role, "content": normalized_content}
        if m.get("name") is not None:
            normalized_message["name"] = m.get("name")

        tool_calls = m.get("tool_calls") if m.get("tool_calls") is not None else m.get("toolCalls")
        tool_call_id = m.get("tool_call_id") if m.get("tool_call_id") is not None else m.get("toolCallId")
        if tool_calls is not None:
            normalized_message["tool_calls"] = tool_calls
        if tool_call_id is not None:
            normalized_message["tool_call_id"] = tool_call_id

        can_merge = set(normalized_message.keys()) == {"role", "content"} and isinstance(normalized_content, str)
        prev_can_merge = (
            bool(out)
            and set(out[-1].keys()) == {"role", "content"}
            and isinstance(out[-1].get("content"), str)
        )
        if last_role is not None and last_role == role and out and can_merge and prev_can_merge:
            prev = out[-1]
            prev_content = prev.get("content") or ""
            prev["content"] = prev_content + "\n" + normalized_content
        else:
            out.append(normalized_message)
            last_role = role
    return out


def _normalize_openai_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    out_function: dict[str, Any] = {"name": name.strip()}

    description = function.get("description")
    if isinstance(description, str):
        out_function["description"] = description

    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        out_function["parameters"] = parameters
    else:
        out_function["parameters"] = {"type": "object", "properties": {}}

    strict = function.get("strict")
    if isinstance(strict, bool):
        out_function["strict"] = strict

    return {"type": "function", "function": out_function}


def _normalize_tool_choice(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = str(tool_choice.get("type") or "").strip().lower()
    if choice_type != "function":
        return {"type": choice_type} if choice_type else tool_choice
    function = tool_choice.get("function")
    if not isinstance(function, dict):
        return {"type": "function"}
    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"type": "function"}
    return {"type": "function", "function": {"name": name.strip()}}


def _normalize_openai_tools_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    tools = payload.get("tools")
    tool_choice = payload.get("tool_choice")
    if not isinstance(tools, list) and not isinstance(tool_choice, dict):
        return payload

    out = dict(payload)
    if isinstance(tools, list):
        normalized_tools = []
        for tool in tools:
            normalized_tool = _normalize_openai_tool(tool)
            if normalized_tool is not None:
                normalized_tools.append(normalized_tool)
        out["tools"] = normalized_tools

    if isinstance(tool_choice, dict):
        out["tool_choice"] = _normalize_tool_choice(tool_choice)

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


async def _http_status_error_detail(exc: httpx.HTTPStatusError, *, upstream: str) -> Dict[str, Any]:
    response = exc.response
    status = response.status_code if response is not None else None
    body = ""
    if response is not None:
        try:
            body = response.text
        except httpx.ResponseNotRead:
            try:
                raw = await response.aread()
            except Exception:
                raw = b""
            body = raw.decode(response.encoding or "utf-8", errors="replace")
    return {"upstream": upstream, "status": status, "body": body[:5000]}


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


def _model_uses_qwen3_thinking_template(model_name: str) -> bool:
    value = (model_name or "").strip().lower()
    return "qwen3" in value


def _model_uses_glm_thinking_template(model_name: str) -> bool:
    value = (model_name or "").strip().lower()
    return "glm-5.2" in value or "glm-5" in value


def _apply_backend_generation_defaults(payload: Dict[str, Any], *, backend_name: str, model_name: str) -> Dict[str, Any]:
    provider = backend_provider_name(backend_name)
    if provider == "vllm" and _model_uses_qwen3_thinking_template(model_name):
        out = dict(payload)
        kwargs = out.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
        kwargs.setdefault("enable_thinking", False)
        out["chat_template_kwargs"] = kwargs
        out.setdefault("repetition_penalty", 1.12)
        return out
    if provider == "mlx" and _model_uses_glm_thinking_template(model_name):
        out = dict(payload)
        kwargs = out.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
        kwargs.setdefault("enable_thinking", False)
        out["chat_template_kwargs"] = kwargs
        return out
    return payload


def _mlx_glm_input_chars(req: ChatCompletionRequest) -> int:
    payload = req.model_dump(
        include={"messages", "tools", "tool_choice", "response_format", "chat_template_kwargs"},
        exclude_none=True,
    )
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))


def _enforce_mlx_glm_input_limit(
    req: ChatCompletionRequest,
    *,
    backend_name: str,
    model_name: str,
) -> None:
    if backend_provider_name(backend_name) != "mlx":
        return

    configured_glm_models = {
        str(getattr(S, "MLX_MODEL_STRONG", "") or "").strip().lower(),
        str(getattr(S, "MLX_MODEL_DEFAULT", "") or "").strip().lower(),
    }
    normalized_model = str(model_name or "").strip().lower()
    if normalized_model not in configured_glm_models and "glm-5.2" not in normalized_model:
        return

    try:
        limit = int(getattr(S, "MLX_GLM_MAX_INPUT_CHARS", 60_000) or 0)
    except Exception:
        limit = 60_000
    if limit <= 0:
        return

    input_chars = _mlx_glm_input_chars(req)
    if input_chars <= limit:
        return

    raise HTTPException(
        status_code=400,
        detail={
            "error": "mlx_glm_input_too_large",
            "model": model_name,
            "input_chars": input_chars,
            "max_input_chars": limit,
            "message": (
                "GLM-5.2 input exceeds the interactive latency guard. "
                "Start a new or compacted conversation, reduce attached tool/file context, "
                "or select a different model."
            ),
        },
    )


def _alias_matches_backend(alias: Any, *, backend_name: str) -> bool:
    registry = get_registry()
    alias_backend = registry.resolve_backend_class(alias.backend) or alias.backend
    resolved_backend = registry.resolve_backend_class(backend_name) or backend_name
    return alias_backend == resolved_backend


def _alias_cap_value(alias: Any, *, backend_name: str) -> int | None:
    if alias is None or alias.max_tokens_cap is None:
        return None
    if not _alias_matches_backend(alias, backend_name=backend_name):
        return None
    try:
        cap = int(alias.max_tokens_cap)
    except Exception:
        return None
    return cap if cap > 0 else None


def _alias_max_tokens_cap(req: ChatCompletionRequest, *, backend_name: str, model_name: str) -> int | None:
    requested_alias = get_alias(str(req.model or "").strip().lower())
    cap = _alias_cap_value(requested_alias, backend_name=backend_name)
    if cap is not None:
        return cap

    normalized_model = str(model_name or "").strip()
    caps: list[int] = []
    for alias in get_aliases().values():
        if str(alias.upstream_model or "").strip() != normalized_model:
            continue
        candidate_cap = _alias_cap_value(alias, backend_name=backend_name)
        if candidate_cap is not None:
            caps.append(candidate_cap)
    if not caps:
        return None
    return max(caps)


def _bounded_max_tokens(
    req: ChatCompletionRequest,
    *,
    backend_name: str,
    model_name: str,
    default_missing: bool,
) -> int | None:
    cap = _alias_max_tokens_cap(req, backend_name=backend_name, model_name=model_name)
    if cap is None:
        return req.max_tokens
    if req.max_tokens is None:
        return cap if default_missing else None
    try:
        requested = int(req.max_tokens)
    except Exception:
        return cap
    return min(requested, cap)


def route_request_for_backend(req: ChatCompletionRequest, backend_name: str, model_name: str) -> ChatCompletionRequest:
    _resolved, provider, _base_url = _resolve_backend_target(backend_name)
    if provider not in {"mlx", "vllm"}:
        return req
    _enforce_mlx_glm_input_limit(req, backend_name=_resolved, model_name=model_name)
    updates: Dict[str, Any] = {"model": model_name}
    max_tokens = _bounded_max_tokens(
        req,
        backend_name=_resolved,
        model_name=model_name,
        default_missing=(provider == "mlx"),
    )
    if max_tokens is not None:
        updates["max_tokens"] = max_tokens
    return req.model_copy(update=updates)


async def call_openai_chat(
    req: ChatCompletionRequest,
    *,
    base_url: str | None = None,
    backend_name: str = "local_vllm",
) -> Dict[str, Any]:
    payload = req.model_dump(exclude_none=True)
    if "messages" in payload and isinstance(payload["messages"], list):
        payload["messages"] = _normalize_messages_for_openai_backend(payload["messages"])
    payload = _normalize_openai_tools_payload(payload)
    payload = _apply_backend_generation_defaults(payload, backend_name=backend_name, model_name=req.model)

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


def _safe_exception_text(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:500]


async def stream_openai_chat(
    payload: Dict[str, Any],
    *,
    base_url: str | None = None,
    backend_name: str = "local_vllm",
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    if "messages" in payload and isinstance(payload["messages"], list):
        payload = dict(payload)
        payload["messages"] = _normalize_messages_for_openai_backend(payload["messages"])
    payload = _normalize_openai_tools_payload(payload)
    payload = _apply_backend_generation_defaults(payload, backend_name=backend_name, model_name=str(payload.get("model") or ""))

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
                async for chunk in passthrough_sse(r, request_id=request_id):
                    yield chunk
        except httpx.HTTPStatusError as e:
            detail = await _http_status_error_detail(e, upstream=backend_name)
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()
        except httpx.RequestError as e:
            detail = {"upstream": backend_name, "error": str(e)}
            yield sse({"error": {"message": "Upstream error", "type": "upstream_error", "param": None, "code": None, "detail": detail}})
            yield sse_done()
        except Exception as e:
            req_id = request_id or "-"
            logger.exception(
                "chat.completions stream failed request_id=%s backend=%s model=%s",
                req_id,
                backend_name,
                payload.get("model"),
            )
            detail = {
                "upstream": backend_name,
                "error": _safe_exception_text(e),
                "request_id": request_id,
            }
            yield sse(
                {
                    "error": {
                        "message": f"Gateway streaming error: {_safe_exception_text(e)}; request_id={req_id}",
                        "type": "server_error",
                        "param": None,
                        "code": "500",
                        "detail": detail,
                    }
                }
            )
            yield sse_done()


async def call_backend_chat(req: ChatCompletionRequest, backend_name: str, model_name: str) -> Dict[str, Any]:
    resolved, _provider, base_url = _resolve_backend_target(backend_name)
    routed_req = route_request_for_backend(req, resolved, model_name)
    return await call_openai_chat(routed_req, base_url=base_url, backend_name=resolved)


def stream_backend_chat_as_openai(
    req: ChatCompletionRequest,
    backend_name: str,
    model_name: str,
    *,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    resolved, _provider, base_url = _resolve_backend_target(backend_name)
    routed_req = route_request_for_backend(req, resolved, model_name)
    payload = routed_req.model_dump(exclude_none=True)
    payload["model"] = model_name
    payload["stream"] = True
    return stream_openai_chat(payload, base_url=base_url, backend_name=resolved, request_id=request_id)
