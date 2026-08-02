from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.auth import enforce_token_model_allowlist, require_bearer
from app.agent_api.auth import agent_tool_caller_from_request
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
from app.openai_utils import new_id, normalize_tool_calls_for_openai, now_unix, sse, sse_done
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
from app import memory_v2, mlx_huge_lane
from app.tool_calling.capabilities import tool_calling_diagnostics
from app.tool_calling.executor import (
    prepare_tools,
    request_has_tool_intent,
    resolve_execution_policy,
    run_gateway_tool_loop,
    tool_intent_param,
)
from app.tool_calling.streaming import stream_final_chat_response
from app.souls import apply_alias_soul


router = APIRouter()


async def _stream_with_admission_release(
    source: AsyncIterator[bytes],
    admission: Any,
    backend_class: str,
) -> AsyncIterator[bytes]:
    """Hold a backend chat slot for the full lifetime of an SSE response."""
    try:
        async for chunk in source:
            yield chunk
    finally:
        admission.release(backend_class, "chat")


@router.get("/v1/tool-calling/diagnostics")
async def tool_calling_diagnostics_route(req: Request):
    require_bearer(req)
    return {"object": "list", "data": tool_calling_diagnostics()}


_ALIAS_IN_REASON = re.compile(r"\balias:([a-z0-9_\-]+)\b", re.IGNORECASE)


def _selected_alias_name(request_model: str, route_reason: str) -> Optional[str]:
    aliases = get_aliases()
    m = _ALIAS_IN_REASON.search(route_reason or "")
    if m:
        cand = (m.group(1) or "").strip().lower()
        if cand in aliases:
            return cand
    key = (request_model or "").strip().lower()
    if key and key in aliases:
        return key
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
    safe_message = str(message or "").strip() or "Gateway request failed"
    error: Dict[str, Any] = {
        "message": safe_message,
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


def _request_needs_tool_route(cc: ChatCompletionRequest, alias_config: Any = None) -> bool:
    if request_has_tool_intent(cc):
        return True
    extension = cc.x_nexus if isinstance(cc.x_nexus, dict) else {}
    requested_mode = str(extension.get("tool_execution_mode") or "").strip().lower()
    if requested_mode == "gateway_exec":
        return True
    return bool(
        alias_config is not None
        and getattr(alias_config, "tool_mode_explicit", False)
        and str(getattr(alias_config, "tool_mode", "") or "").strip().lower() == "gateway_exec"
    )


def _tool_execution_disabled_response(cc: ChatCompletionRequest, *, request_id: str) -> JSONResponse:
    return _openai_error_response(
        "Tool use is disabled for this request; omit tools and parallel_tool_calls, and set tool_choice to none or omit it.",
        param=tool_intent_param(cc) or "tools",
        detail={"request_id": request_id, "error": "tool_execution_disabled"},
    )


def _body_keys(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    return sorted(str(key) for key in body.keys())


_OPENAI_CHAT_REQUEST_KEYS = {
    "model",
    "messages",
    "n",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repetition_penalty",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "seed",
    "max_tokens",
    "response_format",
    "user",
    "metadata",
    "store",
    "stream_options",
    "logprobs",
    "top_logprobs",
    "chat_template_kwargs",
    "x_nexus",
    "stream",
}

_OPENAI_COMPLETION_REQUEST_KEYS = {
    "model",
    "prompt",
    "suffix",
    "max_tokens",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "stream",
    "echo",
    "best_of",
    "logprobs",
    "user",
    "seed",
}

_COMPLETION_OPTION_PROMOTIONS = {
    "maxTokens": "max_tokens",
    "max_tokens": "max_tokens",
    "temperature": "temperature",
    "topP": "top_p",
    "top_p": "top_p",
    "topK": "top_k",
    "top_k": "top_k",
    "minP": "min_p",
    "min_p": "min_p",
    "frequencyPenalty": "frequency_penalty",
    "frequency_penalty": "frequency_penalty",
    "presencePenalty": "presence_penalty",
    "presence_penalty": "presence_penalty",
    "parallelToolCalls": "parallel_tool_calls",
    "parallel_tool_calls": "parallel_tool_calls",
    "stop": "stop",
    "seed": "seed",
    "streamOptions": "stream_options",
    "stream_options": "stream_options",
}


class _ContinueRequestNormalizationError(ValueError):
    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


def _content_shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        part_types: list[str] = []
        for item in value[:8]:
            if isinstance(item, dict):
                part_types.append(str(item.get("type") or "object"))
            else:
                part_types.append(type(item).__name__)
        suffix = ",..." if len(value) > 8 else ""
        return f"array[{','.join(part_types)}{suffix}]"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _content_text_length(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        total = 0
        for item in value:
            if isinstance(item, str):
                total += len(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                total += len(item["text"])
        return total
    return 0


def _serialized_length(value: Any) -> int:
    if value is None:
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _tool_names_from_raw_tools(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _tool_top_level_keys_from_raw_tools(tools: Any) -> list[list[str]]:
    if not isinstance(tools, list):
        return []
    out: list[list[str]] = []
    for tool in tools[:20]:
        if isinstance(tool, dict):
            out.append(sorted(str(key) for key in tool.keys()))
        else:
            out.append([type(tool).__name__])
    return out


def _raw_message_summary(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return []
    out: list[dict[str, Any]] = []
    for idx, message in enumerate(body["messages"]):
        if not isinstance(message, dict):
            out.append({"index": idx, "shape": type(message).__name__})
            continue
        out.append(
            {
                "index": idx,
                "role": message.get("role"),
                "content_present": "content" in message,
                "content_shape": _content_shape(message.get("content")),
                "has_tool_calls": "tool_calls" in message,
                "has_toolCalls": "toolCalls" in message,
                "has_tool_call_id": "tool_call_id" in message,
                "has_toolCallId": "toolCallId" in message,
                "has_thinking_role": str(message.get("role") or "").strip().lower() == "thinking",
                "keys": sorted(str(key) for key in message.keys() if key != "content"),
            }
        )
    return out


def _unknown_top_level_fields(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    return sorted(str(key) for key in body.keys() if str(key) not in _OPENAI_CHAT_REQUEST_KEYS)


def _unknown_completion_fields(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return []
    return sorted(str(key) for key in body.keys() if str(key) not in _OPENAI_COMPLETION_REQUEST_KEYS)


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


def _normalize_message_content_for_gateway(content: Any, *, param: str) -> tuple[Any, bool]:
    if not isinstance(content, list):
        return content, False
    text = _normalize_text_content_parts(content)
    if text is None:
        raise _ContinueRequestNormalizationError(
            "unsupported array content; only text and input_text content parts are supported by this gateway",
            param=param,
        )
    return text, True


def _sanitize_openai_tool(tool: Any) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(tool, dict):
        return None, False
    function = tool.get("function")
    if not isinstance(function, dict):
        return None, False
    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, False

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

    sanitized = {"type": "function", "function": out_function}
    return sanitized, sanitized != tool


def _sanitize_openai_tools(tools: Any, *, source: str, actions: list[str]) -> Any:
    if not isinstance(tools, list):
        return tools
    sanitized_tools: list[dict[str, Any]] = []
    sanitized_count = 0
    dropped_count = 0
    for tool in tools:
        sanitized, changed = _sanitize_openai_tool(tool)
        if sanitized is None:
            dropped_count += 1
            continue
        sanitized_tools.append(sanitized)
        if changed:
            sanitized_count += 1
    if sanitized_count:
        actions.append(f"sanitized {source} tools={sanitized_count}")
    if dropped_count:
        actions.append(f"dropped invalid {source} tools={dropped_count}")
    return sanitized_tools


def _normalize_continue_chat_body(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out = dict(body)
    actions: list[str] = []

    completion_options = out.get("completionOptions")
    if isinstance(completion_options, dict):
        if "tools" not in out and isinstance(completion_options.get("tools"), list):
            out["tools"] = completion_options["tools"]
            actions.append("promoted completionOptions.tools")
        elif "tools" in out and isinstance(completion_options.get("tools"), list):
            actions.append("ignored completionOptions.tools because top-level tools were present")
        if "tool_choice" not in out:
            if "toolChoice" in completion_options:
                out["tool_choice"] = completion_options["toolChoice"]
                actions.append("promoted completionOptions.toolChoice")
            elif "tool_choice" in completion_options:
                out["tool_choice"] = completion_options["tool_choice"]
                actions.append("promoted completionOptions.tool_choice")
        for source_key, target_key in _COMPLETION_OPTION_PROMOTIONS.items():
            if target_key in out or source_key not in completion_options:
                continue
            value = completion_options.get(source_key)
            if value is None:
                continue
            out[target_key] = value
            actions.append(f"promoted completionOptions.{source_key} to {target_key}")
        if "reasoning" in completion_options:
            actions.append("ignored completionOptions.reasoning")
        out.pop("completionOptions", None)
        actions.append("dropped completionOptions")

    if "toolChoice" in out:
        if "tool_choice" not in out:
            out["tool_choice"] = out["toolChoice"]
            actions.append("renamed toolChoice to tool_choice")
        out.pop("toolChoice", None)
    if "reasoning" in out:
        out.pop("reasoning", None)
        actions.append("dropped reasoning")

    if isinstance(out.get("tools"), list):
        out["tools"] = _sanitize_openai_tools(out["tools"], source="request", actions=actions)

    messages = out.get("messages")
    if isinstance(messages, list):
        normalized_messages: list[Any] = []
        for idx, message in enumerate(messages):
            if not isinstance(message, dict):
                normalized_messages.append(message)
                continue
            normalized = dict(message)
            role = str(normalized.get("role") or "").strip().lower()
            if role == "thinking":
                actions.append(f"dropped messages[{idx}] role=thinking")
                continue
            if "toolCalls" in normalized:
                if "tool_calls" not in normalized:
                    normalized["tool_calls"] = normalized["toolCalls"]
                    actions.append(f"renamed messages[{idx}].toolCalls to tool_calls")
                normalized.pop("toolCalls", None)
            if "toolCallId" in normalized:
                if "tool_call_id" not in normalized:
                    normalized["tool_call_id"] = normalized["toolCallId"]
                    actions.append(f"renamed messages[{idx}].toolCallId to tool_call_id")
                normalized.pop("toolCallId", None)
            if "content" in normalized:
                normalized_content, flattened = _normalize_message_content_for_gateway(
                    normalized.get("content"),
                    param=f"messages.{idx}.content",
                )
                if flattened:
                    normalized["content"] = normalized_content
                    actions.append(f"flattened messages[{idx}].content text array")
            if role == "assistant" and "tool_calls" in normalized:
                before_tool_calls = normalized.get("tool_calls")
                normalized["tool_calls"] = normalize_tool_calls_for_openai(before_tool_calls)
                if normalized.get("tool_calls") != before_tool_calls:
                    actions.append(f"normalized messages[{idx}].tool_calls")
            if (
                role == "assistant"
                and normalized.get("content") == ""
                and normalized.get("tool_calls") is not None
            ):
                normalized["content"] = None
                actions.append(f"normalized messages[{idx}].assistant tool call content to null")
            normalized_messages.append(normalized)
        out["messages"] = normalized_messages

    return out, actions


def _http_exception_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("error")
        if isinstance(message, str) and message:
            return message
        try:
            return json.dumps(detail, ensure_ascii=False, default=str)[:500]
        except Exception:
            return str(detail)[:500]
    text = str(detail or "").strip()
    return text or f"HTTP {exc.status_code}"


def _diagnostic_exception_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return _validation_error_message(exc)
    if isinstance(exc, HTTPException):
        return _http_exception_message(exc)
    return (str(exc).strip() or type(exc).__name__)[:500]


def _log_openai_request_diagnostics(
    *,
    request_id: str,
    method: str,
    path: str,
    body: Any,
    normalized_body: Any,
    normalization_actions: list[str],
    cc: ChatCompletionRequest,
    alias_name: Optional[str],
    route: Any,
    backend_class: str,
    tool_fields_action: str,
) -> None:
    if not bool(getattr(S, "GATEWAY_DEBUG_OPENAI_REQUESTS", False)):
        return
    completion_options = body.get("completionOptions") if isinstance(body, dict) else None
    completion_options_tools = completion_options.get("tools") if isinstance(completion_options, dict) else None
    raw_tools = body.get("tools") if isinstance(body, dict) else None
    normalized_tools = normalized_body.get("tools") if isinstance(normalized_body, dict) else None
    payload = {
        "request_id": request_id,
        "method": method,
        "path": path,
        "model": cc.model,
        "stream": bool(cc.stream),
        "stream_options_present": cc.stream_options is not None,
        "stream_options_keys": _body_keys(cc.stream_options),
        "max_tokens": cc.max_tokens,
        "temperature": cc.temperature,
        "top_level_keys": _body_keys(body),
        "normalized_top_level_keys": _body_keys(normalized_body),
        "upstream_request_keys": _body_keys(cc.model_dump(exclude_none=True)),
        "message_count": len(cc.messages),
        "message_roles": [message.role for message in cc.messages],
        "raw_message_summary": _raw_message_summary(body),
        "content_shapes": [_content_shape(message.content) for message in cc.messages],
        "content_text_lengths": [_content_text_length(message.content) for message in cc.messages],
        "total_text_length": sum(_content_text_length(message.content) for message in cc.messages),
        "tools_top_level_present": isinstance(raw_tools, list),
        "tools_completion_options_present": isinstance(completion_options_tools, list),
        "tool_top_level_keys": _tool_top_level_keys_from_raw_tools(raw_tools),
        "completion_options_tool_top_level_keys": _tool_top_level_keys_from_raw_tools(completion_options_tools),
        "normalized_tool_top_level_keys": _tool_top_level_keys_from_raw_tools(normalized_tools),
        "has_tools": bool(cc.tools),
        "tool_count": len(cc.tools or []),
        "tool_names": _tool_names_from_raw_tools(normalized_tools),
        "tool_schema_bytes": _serialized_length(normalized_tools),
        "tool_choice_present": cc.tool_choice is not None,
        "response_format_present": cc.response_format is not None,
        "reasoning_present": isinstance(body, dict) and "reasoning" in body,
        "completion_options_keys": _body_keys(completion_options),
        "unknown_top_level_fields": _unknown_top_level_fields(body),
        "normalization_actions": normalization_actions,
        "selected_alias": alias_name,
        "route_backend": getattr(route, "backend", None),
        "backend_class": backend_class,
        "route_model": getattr(route, "model", None),
        "route_reason": getattr(route, "reason", None),
        "tool_fields_action": tool_fields_action,
    }
    logger.info("openai request diagnostics %s", json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _log_openai_response_diagnostics(
    *,
    request_id: str,
    status_code: int,
    stream: bool,
    route: str | None = None,
    phase: str | None = None,
    backend_class: str = "",
    model_name: str = "",
    error_type: str | None = None,
    upstream_status: int | None = None,
    exception: Exception | None = None,
) -> None:
    if not bool(getattr(S, "GATEWAY_DEBUG_OPENAI_REQUESTS", False)):
        return
    payload: dict[str, Any] = {
        "request_id": request_id,
        "status_code": status_code,
        "stream": stream,
        "backend_class": backend_class or None,
        "upstream_model": model_name or None,
    }
    if route:
        payload["route"] = route
    if phase:
        payload["phase"] = phase
    if error_type:
        payload["error_type"] = error_type
    if upstream_status is not None:
        payload["upstream_status"] = upstream_status
    if exception is not None:
        payload["exception_class"] = type(exception).__name__
        payload["exception_message"] = _diagnostic_exception_message(exception)
    logger.info("openai response diagnostics %s", json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _prompt_shape(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        item_types = [type(item).__name__ for item in value[:8]]
        suffix = ",..." if len(value) > 8 else ""
        return f"array[{','.join(item_types)}{suffix}]"
    return type(value).__name__


def _prompt_length(value: Any) -> int | None:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sum(len(item) for item in value)
    return None


def _completion_prompt_to_text(prompt: Any) -> str:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list) and all(isinstance(item, str) for item in prompt):
        return "\n".join(prompt)
    raise _ContinueRequestNormalizationError("prompt must be a string or list of strings", param="prompt")


def _log_openai_completion_request_diagnostics(
    *,
    request_id: str,
    method: str,
    path: str,
    body: Any,
    cr: CompletionRequest,
    alias_name: Optional[str],
    route: Any,
    backend_class: str,
    upstream_keys: list[str],
) -> None:
    if not bool(getattr(S, "GATEWAY_DEBUG_OPENAI_REQUESTS", False)):
        return
    prompt = body.get("prompt") if isinstance(body, dict) else None
    payload = {
        "request_id": request_id,
        "method": method,
        "path": path,
        "route": "/v1/completions",
        "adapter_path": "legacy_completions_shim",
        "model": cr.model,
        "stream": bool(cr.stream),
        "top_level_keys": _body_keys(body),
        "unknown_top_level_fields": _unknown_completion_fields(body),
        "prompt_shape": _prompt_shape(prompt),
        "prompt_length": _prompt_length(prompt),
        "suffix_present": cr.suffix is not None,
        "max_tokens": cr.max_tokens,
        "temperature": cr.temperature,
        "top_p": cr.top_p,
        "stop_present": cr.stop is not None,
        "echo": cr.echo,
        "best_of": cr.best_of,
        "logprobs": cr.logprobs,
        "selected_alias": alias_name,
        "route_backend": getattr(route, "backend", None),
        "backend_class": backend_class,
        "route_model": getattr(route, "model", None),
        "route_reason": getattr(route, "reason", None),
        "normalized_upstream_top_level_keys": upstream_keys,
        "completionOptions_present": isinstance(body, dict) and "completionOptions" in body,
        "completionOptions_tools_present": (
            isinstance(body, dict)
            and isinstance(body.get("completionOptions"), dict)
            and isinstance(body["completionOptions"].get("tools"), list)
        ),
        "reasoning_present": isinstance(body, dict) and "reasoning" in body,
        "response_format_present": isinstance(body, dict) and "response_format" in body,
    }
    logger.info("openai completions diagnostics %s", json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _openai_error_sse(
    message: str,
    *,
    error_type: str = "server_error",
    code: str | None = "500",
    param: str | None = None,
    detail: Any = None,
) -> bytes:
    return sse(_openai_error_payload(message, error_type=error_type, param=param, code=code, detail=detail))


def _request_messages_summary(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        item = {
            "role": message.role,
            "content_shape": _content_shape(message.content),
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


async def _call_backend_chat_with_request_id(
    cc: ChatCompletionRequest,
    backend: str,
    model_name: str,
    *,
    request_id: str,
) -> Dict[str, Any]:
    try:
        return await call_backend_chat(cc, backend, model_name, request_id=request_id)
    except TypeError as exc:
        # Backward-compatible fallback for tests that monkeypatch a 3-arg stub.
        if "request_id" in str(exc):
            return await call_backend_chat(cc, backend, model_name)
        raise


def _tool_choice_uses_guided_decoding(tool_choice: Any) -> bool:
    if isinstance(tool_choice, str):
        return tool_choice.strip().lower() == "required"
    if isinstance(tool_choice, dict):
        normalized = str(tool_choice.get("type") or "").strip().lower()
        if normalized == "required":
            return True
        if normalized != "function":
            return False
        function = tool_choice.get("function")
        return isinstance(function, dict) and bool(str(function.get("name") or "").strip())
    return False


def _backend_is_vllm(backend_class: str) -> bool:
    try:
        cfg = get_registry().get_backend(backend_class)
        provider = str(getattr(cfg, "provider", "") or "").strip().lower() if cfg is not None else ""
        if provider:
            return provider == "vllm"
    except Exception:
        pass

    normalized = (backend_class or "").strip().lower().replace("-", "_")
    return normalized in {"vllm", "local_vllm", "local_vllm_fast"} or normalized.startswith("vllm_")


def _backend_supports_guided_tool_choice(backend_class: str, tool_choice: Any) -> bool:
    return _backend_is_vllm(backend_class) and _tool_choice_uses_guided_decoding(tool_choice)


def _degradation_reason(
    *,
    alias_name: Optional[str],
    backend_class: str,
    backend_supports_tools: bool,
    tool_qualification_reason: Optional[str] = None,
) -> Optional[str]:
    reasons: list[str] = []
    if alias_name and not _alias_allows_tools(alias_name):
        reasons.append(f"model alias '{alias_name}' disables tools")
    if not backend_supports_tools:
        reasons.append(f"backend '{backend_class}' does not support native tool calling")
    if tool_qualification_reason:
        reasons.append(f"tool qualification guardrail: {tool_qualification_reason}")
    if not reasons:
        return None
    return "; ".join(reasons)


def _normalize_chat_request_for_backend(
    cc: ChatCompletionRequest,
    *,
    alias_name: Optional[str],
    backend_class: str,
    model_name: str = "",
    enforce_tool_qualification: bool = True,
) -> tuple[ChatCompletionRequest, Optional[str], bool]:
    if not _tool_fields_present(cc):
        return cc, None, False

    alias_allows_tools = _alias_allows_tools(alias_name)
    backend_supports_native_tools = backend_supports_tool_calling(backend_class)
    backend_supports_guided_tools = _backend_supports_guided_tool_choice(backend_class, cc.tool_choice)
    backend_supports_tools = backend_supports_native_tools or backend_supports_guided_tools
    tool_qualification_reason: Optional[str] = None
    if alias_allows_tools and backend_supports_tools and enforce_tool_qualification:
        try:
            from app import model_tool_qualification

            tool_qualification_reason = model_tool_qualification.guardrail_reason_for_target(
                alias_name=alias_name,
                backend_class=backend_class,
                resolved_model=model_name or cc.model,
                tool_choice=cc.tool_choice if cc.tool_choice is not None else "auto",
                has_tools=bool(cc.tools),
            )
        except Exception as exc:
            logger.info(
                "chat.completions tool qualification guardrail skipped alias=%s backend=%s model=%s error=%s:%s",
                alias_name or "-",
                backend_class,
                model_name or cc.model,
                type(exc).__name__,
                exc,
            )

    if alias_allows_tools and backend_supports_tools and not tool_qualification_reason:
        if backend_supports_guided_tools and not backend_supports_native_tools:
            logger.info(
                "chat.completions passing required/named tool fields via vllm guided decoding alias=%s backend=%s tool_choice=%r",
                alias_name or "-",
                backend_class,
                cc.tool_choice,
            )
        return cc, None, False

    degradation_reason = _degradation_reason(
        alias_name=alias_name,
        backend_class=backend_class,
        backend_supports_tools=backend_supports_tools,
        tool_qualification_reason=tool_qualification_reason,
    )

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


_BACKENDS_WITH_UNSUPPORTED_STREAM_OPTIONS = {"local_mlx", "local_mlx_migraine"}


def _normalize_stream_options_for_backend(
    cc: ChatCompletionRequest,
    *,
    alias_name: Optional[str],
    backend_class: str,
) -> tuple[ChatCompletionRequest, Optional[str]]:
    if cc.stream_options is None:
        return cc, None
    if backend_class not in _BACKENDS_WITH_UNSUPPORTED_STREAM_OPTIONS:
        return cc, None

    logger.info(
        "chat.completions dropping stream_options alias=%s backend=%s keys=%s reason=unsupported_by_backend",
        alias_name or "-",
        backend_class,
        _body_keys(cc.stream_options),
    )
    return (
        cc.model_copy(update={"stream_options": None}),
        f"dropped stream_options for backend '{backend_class}'",
    )


def _should_buffer_tool_call_stream(cc: ChatCompletionRequest, alias_config: Any) -> bool:
    return bool(
        cc.stream
        and cc.tools
        and alias_config is not None
        and getattr(alias_config, "buffer_tool_call_stream", False)
    )


def _route_chat_request(
    cc: ChatCompletionRequest,
    *,
    headers: Dict[str, str],
    enable_request_type: bool = False,
) -> tuple[Any, str, Optional[str]]:
    requested_huge_model = mlx_huge_lane.resolve_request_model(cc.model)
    if requested_huge_model:
        block = mlx_huge_lane.request_block(requested_huge_model)
        if block:
            status_code = 503 if block.get("retryable") or block.get("error") == "mlx_huge_transition_failed" else 409
            raise HTTPException(status_code=status_code, detail=block)
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
    alias_name = _selected_alias_name(cc.model, route.reason)
    aliases = get_aliases()
    alias_config = aliases.get(alias_name or "")
    fallback_alias = str(getattr(alias_config, "tool_fallback_alias", "") or "").strip().lower()
    if _request_needs_tool_route(cc, alias_config) and fallback_alias and fallback_alias != alias_name:
        fallback_config = aliases.get(fallback_alias)
        if fallback_config is not None and fallback_config.tools is not False:
            fallback_route = decide_route(
                cfg=router_cfg(),
                request_model=fallback_alias,
                headers=headers,
                messages=[m.model_dump(exclude_none=True) for m in cc.messages],
                has_tools=False,
                enable_policy=S.ROUTER_ENABLE_POLICY,
                enable_request_type=enable_request_type,
            )
            logger.info(
                "tool route fallback alias=%s fallback_alias=%s backend=%s model=%s",
                alias_name,
                fallback_alias,
                fallback_route.backend,
                fallback_route.model,
            )
            route = fallback_route

    backend_class = get_registry().resolve_backend_class(route.backend)
    if backend_class == "local_mlx":
        block = mlx_huge_lane.request_block(route.model)
        if block:
            status_code = 503 if block.get("retryable") or block.get("error") == "mlx_huge_transition_failed" else 409
            raise HTTPException(status_code=status_code, detail=block)
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
    request_id = str(getattr(req.state, "request_id", "") or "-")
    try:
        body = await req.json()
    except Exception as exc:
        _log_openai_response_diagnostics(request_id=request_id, status_code=400, stream=False, error_type="invalid_json", exception=exc)
        return _openai_error_response(
            f"Unsupported field or invalid request shape: invalid JSON body ({type(exc).__name__}); request_id={request_id}",
            detail={"request_id": request_id},
        )

    if not isinstance(body, dict):
        _log_openai_response_diagnostics(request_id=request_id, status_code=400, stream=False, error_type="invalid_json_shape")
        return _openai_error_response(
            f"Unsupported field or invalid request shape: JSON body must be an object; request_id={request_id}",
            detail={"request_id": request_id},
        )

    try:
        normalized_body, normalization_actions = _normalize_continue_chat_body(body)
    except _ContinueRequestNormalizationError as exc:
        _log_openai_response_diagnostics(request_id=request_id, status_code=400, stream=False, error_type="normalization_error", exception=exc)
        return _openai_error_response(
            f"Unsupported field or invalid request shape: {exc}; request_id={request_id}",
            param=exc.param,
            detail={"request_id": request_id},
        )

    try:
        cc = ChatCompletionRequest(**normalized_body)
    except ValidationError as exc:
        _log_openai_response_diagnostics(request_id=request_id, status_code=400, stream=False, error_type="validation_error", exception=exc)
        return _openai_error_response(
            f"{_validation_error_message(exc)}; request_id={request_id}",
            param=_validation_error_param(exc),
            detail={"request_id": request_id, "errors": exc.errors(include_url=False)},
        )

    enforce_token_model_allowlist(req, cc.model)

    # Disabled tool execution is a request boundary, not a backend capability.
    # Enforce it before routing, normalization, admission, or upstream dispatch.
    try:
        requested_alias = get_aliases().get(cc.model)
        preflight_policy = resolve_execution_policy(cc, requested_alias)
        if preflight_policy.mode == "disabled":
            if request_has_tool_intent(cc):
                return _tool_execution_disabled_response(cc, request_id=request_id)
            cc = prepare_tools(cc, preflight_policy, requested_alias)
    except (TypeError, ValueError) as exc:
        return _openai_error_response(
            f"Invalid Nexus tool execution policy: {exc}",
            param="x_nexus.tool_execution_mode",
            detail={"request_id": request_id, "error": "invalid_tool_execution_policy"},
        )

    admission = None
    backend_class = ""
    backend = ""
    model_name = ""
    route = None
    alias_name: Optional[str] = None
    tool_fields_action = "pending"
    degradation_reason: Optional[str] = None
    execution_policy = None
    requested_stream = bool(cc.stream)

    try:
        requested_huge_model = mlx_huge_lane.resolve_request_model(cc.model)
        if requested_huge_model:
            block = mlx_huge_lane.request_block(requested_huge_model)
            if block:
                status_code = 503 if block.get("retryable") or block.get("error") == "mlx_huge_transition_failed" else 409
                raise HTTPException(status_code=status_code, detail=block)
        cc.messages = await inject_memory(cc.messages, req=req)

        hdrs = {k.lower(): v for k, v in req.headers.items()}
        route, backend_class, alias_name = _route_chat_request(
            cc,
            headers=hdrs,
            enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
        )
        backend = route.backend
        model_name = route.model

        cc = _apply_alias_constraints(cc, alias_name=alias_name)
        alias_config = get_aliases().get(alias_name) if alias_name else None
        cc = apply_alias_soul(cc, alias_config)
        try:
            execution_policy = resolve_execution_policy(cc, alias_config)
            if execution_policy.mode in {"gateway_exec", "disabled"}:
                if execution_policy.mode == "disabled" and request_has_tool_intent(cc):
                    return _tool_execution_disabled_response(cc, request_id=request_id)
                cc = prepare_tools(cc, execution_policy, alias_config)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_tool_execution_policy", "message": str(exc)}) from exc

        # Check backend health/readiness
        check_backend_ready(backend_class, route_kind="chat")

        # Check capability
        await check_capability(backend_class, "chat")

        # Acquire admission slot
        admission = get_admission_controller()
        await admission.acquire(backend_class, "chat")

        original_cc = cc

        cc, degradation_reason, tools_required_error = _normalize_chat_request_for_backend(
            cc,
            alias_name=alias_name,
            backend_class=backend_class,
            model_name=model_name,
        )
        cc, stream_options_action = _normalize_stream_options_for_backend(
            cc,
            alias_name=alias_name,
            backend_class=backend_class,
        )
        if stream_options_action:
            normalization_actions.append(stream_options_action)
        request_shape_action = "normalized" if normalization_actions else "direct"
        tool_fields_action = _tool_fields_action(
            original_cc,
            cc,
            request_shape_action=request_shape_action,
        )
        _log_openai_request(
            endpoint="/v1/chat/completions",
            body=body,
            cc=cc,
            alias_name=alias_name,
            route=route,
            tool_fields_action=tool_fields_action,
            request_shape_action=request_shape_action,
        )
        _log_openai_request_diagnostics(
            request_id=request_id,
            method=req.method,
            path=str(req.url.path),
            body=body,
            normalized_body=normalized_body,
            normalization_actions=normalization_actions,
            cc=cc,
            alias_name=alias_name,
            route=route,
            backend_class=backend_class,
            tool_fields_action=tool_fields_action,
        )

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
                    "normalized_request_keys": _body_keys(normalized_body),
                    "selected_alias": alias_name,
                    "stream": bool(cc.stream),
                    "tool_choice": cc.tool_choice,
                    "tool_fields_action": tool_fields_action,
                    "normalization_actions": normalization_actions,
                    "message_roles": [message.role for message in cc.messages],
                    "content_shapes": [_content_shape(message.content) for message in cc.messages],
                }
            )
            req.state.instrument = inst
        except Exception:
            pass

        if tools_required_error:
            _log_openai_response_diagnostics(
                request_id=request_id,
                status_code=400,
                stream=bool(cc.stream),
                backend_class=backend_class,
                model_name=model_name,
                error_type="tools_required_error",
            )
            return _openai_error_response(
                f"Unsupported field or invalid request shape: tools were explicitly required, but {degradation_reason}; request_id={request_id}",
                param="tool_choice",
                detail={
                    "request_id": request_id,
                    "backend": backend_class,
                    "alias": alias_name,
                    "tool_choice": cc.tool_choice,
                },
            )

        if execution_policy is not None and execution_policy.mode == "gateway_exec" and degradation_reason:
            return _openai_error_response(
                f"Gateway tool execution is unavailable because {degradation_reason}; request_id={request_id}",
                param="tools",
                detail={
                    "request_id": request_id,
                    "backend": backend_class,
                    "alias": alias_name,
                    "degradation_reason": degradation_reason,
                },
            )

        logger.debug(
            "route chat.completions request_id=%s model=%r alias=%r stream=%s tools=%s tool_choice=%r tool_fields_action=%s degraded_tools=%s -> backend=%s upstream_model=%s reason=%s",
            request_id,
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

        if execution_policy is not None and execution_policy.mode == "gateway_exec":
            async def gateway_exec_call(loop_req: ChatCompletionRequest) -> Dict[str, Any]:
                return await _call_backend_chat_with_request_id(loop_req, backend, model_name, request_id=request_id)
            t0 = time.monotonic()
            loop_result = await run_gateway_tool_loop(
                cc,
                policy=execution_policy,
                alias=alias_config,
                call_backend=gateway_exec_call,
                request_id=request_id,
                caller=agent_tool_caller_from_request(req),
            )
            resp = loop_result.response
            try:
                inst = getattr(req.state, "instrument", None)
                if isinstance(inst, dict):
                    inst["gateway_tool_loop_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                    inst["tool_execution_mode"] = "gateway_exec"
                    inst["tool_calls_returned"] = list(loop_result.calls_seen)
                    inst["tools_executed"] = list(loop_result.tools_executed)
                    inst["tool_rounds"] = loop_result.rounds
            except Exception:
                pass
            if requested_stream:
                out = StreamingResponse(stream_final_chat_response(resp), media_type="text/event-stream")
            else:
                out = JSONResponse(resp)
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            out.headers["X-Nexus-Tool-Execution"] = "gateway_exec"
            out.headers["X-Nexus-Tools-Executed"] = ",".join(loop_result.tools_executed)
            out.headers["X-Nexus-Tool-Rounds"] = str(loop_result.rounds)
            return out

        if _should_buffer_tool_call_stream(cc, alias_config):
            logger.info(
                "chat.completions buffering tool stream alias=%s backend=%s reason=profile_requires_nonstream_parser",
                alias_name or "-",
                backend_class,
            )
            buffered_cc = cc.model_copy(update={"stream": False, "stream_options": None})
            t0 = time.monotonic()
            resp = await _call_backend_chat_with_request_id(
                buffered_cc,
                backend,
                model_name,
                request_id=request_id,
            )
            try:
                inst = getattr(req.state, "instrument", None)
                if isinstance(inst, dict):
                    inst["upstream_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                    inst["tool_stream_buffered"] = True
            except Exception:
                pass
            out = StreamingResponse(stream_final_chat_response(resp), media_type="text/event-stream")
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            out.headers["X-Nexus-Tool-Stream"] = "buffered"
            _log_openai_response_diagnostics(
                request_id=request_id,
                status_code=200,
                stream=True,
                backend_class=backend_class,
                model_name=model_name,
            )
            return out

        if cc.stream:
            upstream_gen = stream_backend_chat_as_openai(cc, backend, model_name, request_id=request_id)
            gen = _stream_with_admission_release(upstream_gen, admission, backend_class)
            out = StreamingResponse(gen, media_type="text/event-stream")
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            _log_openai_response_diagnostics(
                request_id=request_id,
                status_code=200,
                stream=True,
                backend_class=backend_class,
                model_name=model_name,
            )
            # The response iterator now owns the lease. The route-level finally
            # must not release it while the upstream is still generating.
            admission = None
            return out

        t0 = time.monotonic()
        resp = await _call_backend_chat_with_request_id(cc, backend, model_name, request_id=request_id)
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
        _log_openai_response_diagnostics(
            request_id=request_id,
            status_code=200,
            stream=False,
            backend_class=backend_class,
            model_name=model_name,
        )
        return out
    except HTTPException as exc:
        status_code = int(exc.status_code or 500)
        error_type = "server_error" if status_code >= 500 else "invalid_request_error"
        code = str(status_code) if status_code >= 500 else "invalid_request"
        message = _http_exception_message(exc)
        if status_code >= 500:
            logger.exception(
                "chat.completions HTTPException request_id=%s status=%s backend=%s model=%s detail=%s",
                request_id,
                status_code,
                backend_class or "-",
                model_name or "-",
                message,
            )
        _log_openai_response_diagnostics(
            request_id=request_id,
            status_code=status_code,
            stream=False,
            backend_class=backend_class,
            model_name=model_name,
            error_type=error_type,
            exception=exc,
        )
        return _openai_error_response(
            f"Gateway chat completion failed: {message}; request_id={request_id}",
            status_code=status_code,
            error_type=error_type,
            code=code,
            detail={"request_id": request_id, "detail": exc.detail},
        )
    except Exception as exc:
        logger.exception(
            "chat.completions failed request_id=%s backend=%s model=%s",
            request_id,
            backend_class or "-",
            model_name or "-",
        )
        message = str(exc).strip() or type(exc).__name__
        _log_openai_response_diagnostics(
            request_id=request_id,
            status_code=500,
            stream=False,
            backend_class=backend_class,
            model_name=model_name,
            error_type="server_error",
            exception=exc,
        )
        return _openai_error_response(
            f"Gateway failed while handling chat completion: {type(exc).__name__}: {message[:500]}; request_id={request_id}",
            status_code=500,
            error_type="server_error",
            code="500",
            detail={"request_id": request_id},
        )
    finally:
        if admission is not None and backend_class:
            try:
                admission.release(backend_class, "chat")
            except Exception:
                pass


@router.post("/v1/completions")
async def completions(req: Request):
    require_bearer(req)
    request_id = str(getattr(req.state, "request_id", "") or "-")
    route_path = "/v1/completions"
    backend = ""
    backend_class = ""
    model_name = ""
    route = None
    alias_name: Optional[str] = None

    try:
        body = await req.json()
    except Exception as exc:
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="parse_json",
            status_code=400,
            stream=False,
            error_type="invalid_json",
            exception=exc,
        )
        return _openai_error_response(
            f"Unsupported field or invalid request shape: invalid JSON body ({type(exc).__name__}); request_id={request_id}",
            detail={"request_id": request_id},
        )

    if not isinstance(body, dict):
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="validate_shape",
            status_code=400,
            stream=False,
            error_type="invalid_json_shape",
        )
        return _openai_error_response(
            f"Unsupported field or invalid request shape: JSON body must be an object; request_id={request_id}",
            detail={"request_id": request_id},
        )

    try:
        cr = CompletionRequest(**body)
        prompt_text = _completion_prompt_to_text(cr.prompt)
    except ValidationError as exc:
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="validate_request",
            status_code=400,
            stream=bool(body.get("stream")) if isinstance(body, dict) else False,
            error_type="validation_error",
            exception=exc,
        )
        return _openai_error_response(
            f"{_validation_error_message(exc)}; request_id={request_id}",
            param=_validation_error_param(exc),
            detail={"request_id": request_id, "errors": exc.errors(include_url=False)},
        )
    except _ContinueRequestNormalizationError as exc:
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="normalize_prompt",
            status_code=400,
            stream=bool(body.get("stream")),
            error_type="normalization_error",
            exception=exc,
        )
        return _openai_error_response(
            f"Unsupported field or invalid request shape: {exc}; request_id={request_id}",
            param=exc.param,
            detail={"request_id": request_id},
        )

    cc = ChatCompletionRequest(
        model=cr.model,
        messages=[ChatMessage(role="user", content=prompt_text)],
        temperature=cr.temperature,
        top_p=cr.top_p,
        frequency_penalty=cr.frequency_penalty,
        presence_penalty=cr.presence_penalty,
        stop=cr.stop,
        seed=cr.seed,
        max_tokens=cr.max_tokens,
        stream=bool(cr.stream),
    )

    try:
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        route, backend_class, alias_name = _route_chat_request(
            cc,
            headers=hdrs,
            enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
        )
        backend = route.backend
        model_name = route.model

        cc = _apply_alias_constraints(cc, alias_name=alias_name)
        upstream_keys = _body_keys(cc.model_dump(exclude_none=True))
        _log_openai_completion_request_diagnostics(
            request_id=request_id,
            method=req.method,
            path=str(req.url.path),
            body=body,
            cr=cr,
            alias_name=alias_name,
            route=route,
            backend_class=backend_class,
            upstream_keys=upstream_keys,
        )

        try:
            inst = getattr(req.state, "instrument", None)
            if not isinstance(inst, dict):
                inst = {}
            inst.update(
                {
                    "op": "completions",
                    "backend": backend,
                    "backend_class": backend_class,
                    "upstream_model": model_name,
                    "router_reason": route.reason,
                    "selected_alias": alias_name,
                    "stream": bool(cc.stream),
                    "prompt_shape": _prompt_shape(body.get("prompt")),
                    "prompt_length": _prompt_length(body.get("prompt")),
                    "request_keys": _body_keys(body),
                    "normalized_request_keys": upstream_keys,
                }
            )
            req.state.instrument = inst
        except Exception:
            pass

        if cc.stream:
            stream_id = new_id("cmpl")
            created = now_unix()
            used_model_id = backend_model_id(backend, model_name)

            async def gen() -> AsyncIterator[bytes]:
                terminal_seen = False
                try:
                    async for sse_bytes in stream_backend_chat_as_openai(cc, backend, model_name, request_id=request_id):
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
                            if isinstance(j, dict) and isinstance(j.get("error"), dict):
                                err = dict(j["error"])
                                message = str(err.get("message") or "").strip()
                                if not message:
                                    message = f"Gateway request failed; request_id={request_id}; route={route_path}; phase=stream; error=empty upstream error"
                                err["message"] = message
                                err.setdefault("type", "server_error")
                                err.setdefault("param", None)
                                err.setdefault("code", "500")
                                yield sse({"error": err})
                                yield sse_done()
                                return

                            choice = ((j or {}).get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            text = delta.get("content")
                            if isinstance(text, str) and text:
                                yield sse(
                                    {
                                        "id": stream_id,
                                        "object": "text_completion.chunk",
                                        "created": created,
                                        "model": used_model_id,
                                        "choices": [{"index": 0, "text": text, "finish_reason": None}],
                                    }
                                )
                            finish_reason = choice.get("finish_reason")
                            if finish_reason is not None and not terminal_seen:
                                terminal_seen = True
                                yield sse(
                                    {
                                        "id": stream_id,
                                        "object": "text_completion.chunk",
                                        "created": created,
                                        "model": used_model_id,
                                        "choices": [{"index": 0, "text": "", "finish_reason": finish_reason}],
                                    }
                                )
                except asyncio.CancelledError:
                    logger.info("completions stream cancelled request_id=%s backend=%s model=%s", request_id, backend_class or "-", model_name or "-")
                    return
                except HTTPException as exc:
                    status_code = int(exc.status_code or 500)
                    error_type = "server_error" if status_code >= 500 else "invalid_request_error"
                    code = str(status_code) if status_code >= 500 else "invalid_request"
                    message = _http_exception_message(exc)
                    if status_code >= 500:
                        logger.exception(
                            "completions stream HTTPException request_id=%s status=%s backend=%s model=%s detail=%s",
                            request_id,
                            status_code,
                            backend_class or "-",
                            model_name or "-",
                            message,
                        )
                    yield _openai_error_sse(
                        f"Gateway request failed; request_id={request_id}; route={route_path}; phase=stream; error=HTTPException: {message}",
                        error_type=error_type,
                        code=code,
                        detail={"request_id": request_id, "detail": exc.detail},
                    )
                    yield sse_done()
                    return
                except Exception as exc:
                    logger.exception(
                        "completions stream failed request_id=%s backend=%s model=%s",
                        request_id,
                        backend_class or "-",
                        model_name or "-",
                    )
                    message = str(exc).strip() or type(exc).__name__
                    yield _openai_error_sse(
                        f"Gateway request failed; request_id={request_id}; route={route_path}; phase=stream; error={type(exc).__name__}: {message[:500]}",
                        error_type="server_error",
                        code="500",
                        detail={"request_id": request_id},
                    )
                    yield sse_done()
                    return

                if not terminal_seen:
                    yield sse(
                        {
                            "id": stream_id,
                            "object": "text_completion.chunk",
                            "created": created,
                            "model": used_model_id,
                            "choices": [{"index": 0, "text": "", "finish_reason": "stop"}],
                        }
                    )
                yield sse_done()

            out = StreamingResponse(gen(), media_type="text/event-stream")
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            _log_openai_response_diagnostics(
                request_id=request_id,
                route=route_path,
                phase="stream_start",
                status_code=200,
                stream=True,
                backend_class=backend_class,
                model_name=model_name,
            )
            return out

        t0 = time.monotonic()
        chat_resp = await _call_backend_chat_with_request_id(cc, backend, model_name, request_id=request_id)
        try:
            inst = getattr(req.state, "instrument", None)
            if isinstance(inst, dict):
                inst["upstream_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        except Exception:
            pass

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
            "usage": chat_resp.get("usage") if isinstance(chat_resp.get("usage"), dict) else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

        out = JSONResponse(resp)
        out.headers["X-Backend-Used"] = backend
        out.headers["X-Model-Used"] = model_name
        out.headers["X-Router-Reason"] = route.reason
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="complete",
            status_code=200,
            stream=False,
            backend_class=backend_class,
            model_name=model_name,
        )
        return out
    except HTTPException as exc:
        status_code = int(exc.status_code or 500)
        error_type = "server_error" if status_code >= 500 else "invalid_request_error"
        code = str(status_code) if status_code >= 500 else "invalid_request"
        message = _http_exception_message(exc)
        if status_code >= 500:
            logger.exception(
                "completions HTTPException request_id=%s status=%s backend=%s model=%s detail=%s",
                request_id,
                status_code,
                backend_class or "-",
                model_name or "-",
                message,
            )
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="route_or_upstream",
            status_code=status_code,
            stream=bool(cc.stream),
            backend_class=backend_class,
            model_name=model_name,
            error_type=error_type,
            exception=exc,
        )
        return _openai_error_response(
            f"Gateway request failed; request_id={request_id}; route={route_path}; phase=route_or_upstream; error=HTTPException: {message}",
            status_code=status_code,
            error_type=error_type,
            code=code,
            detail={"request_id": request_id, "detail": exc.detail},
        )
    except Exception as exc:
        logger.exception(
            "completions failed request_id=%s backend=%s model=%s",
            request_id,
            backend_class or "-",
            model_name or "-",
        )
        message = str(exc).strip() or type(exc).__name__
        _log_openai_response_diagnostics(
            request_id=request_id,
            route=route_path,
            phase="route_or_upstream",
            status_code=500,
            stream=bool(cc.stream),
            backend_class=backend_class,
            model_name=model_name,
            error_type="server_error",
            exception=exc,
        )
        return _openai_error_response(
            f"Gateway request failed; request_id={request_id}; route={route_path}; phase=route_or_upstream; error={type(exc).__name__}: {message[:500]}",
            status_code=500,
            error_type="server_error",
            code="500",
            detail={"request_id": request_id},
        )


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
    request_id = str(getattr(req.state, "request_id", "") or "-")
    try:
        body = await req.json()
    except Exception as exc:
        return _openai_error_response(
            f"Unsupported field or invalid request shape: invalid JSON body ({type(exc).__name__})"
        )
    if not isinstance(body, dict):
        return _openai_error_response("Unsupported field or invalid request shape: body must be an object")

    response_extension = body.get("x_nexus")
    if isinstance(response_extension, dict) and str(response_extension.get("tool_execution_mode") or "").strip().lower() == "gateway_exec":
        return _openai_error_response(
            "Gateway-side tool execution is not supported by /v1/responses; use /v1/chat/completions",
            param="x_nexus.tool_execution_mode",
        )

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
            model_name=model_name,
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

            leased_gen = _stream_with_admission_release(gen(), admission, backend_class)
            out = StreamingResponse(leased_gen, media_type="text/event-stream")
            out.headers["X-Backend-Used"] = backend
            out.headers["X-Model-Used"] = model_name
            out.headers["X-Router-Reason"] = route.reason
            admission = None
            return out

        chat_resp = await _call_backend_chat_with_request_id(cc, backend, model_name, request_id=request_id)

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
        if admission is not None:
            admission.release(backend_class, "chat")
