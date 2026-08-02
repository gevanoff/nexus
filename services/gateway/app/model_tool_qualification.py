from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel

from app.backends import backend_supports_tool_calling, check_capability, get_admission_controller, get_registry
from app.config import S, logger
from app.health_checker import check_backend_ready
from app.model_benchmark import clean_model_list, error_text, iter_sse_payloads
from app.models import ChatCompletionRequest, ChatMessage
from app.openai_utils import now_unix, tool_call_name_error
from app.router import decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_backend_chat, stream_backend_chat_as_openai


SCHEMA_VERSION = "nexus.model_tool_qualification.v1"

MAX_MODELS_PER_RUN = 12
MAX_MAX_TOKENS = 512
RECENT_HISTORY_LINES = 2500
TOOL_NAME = "get_weather"
TOOL_RESULT_MARKER = "TOOL_RESULT_OK"

_QUALIFICATION_LOCK = asyncio.Lock()
_SCHEDULER_TASK: Optional[asyncio.Task] = None
_SCHEDULER_STOP: Optional[asyncio.Event] = None


class ToolQualificationBusy(RuntimeError):
    pass


class ModelToolQualificationRequest(BaseModel):
    models: List[str]
    include_stream: bool = True
    include_roundtrip: bool = True
    max_tokens: int = 96
    temperature: float = 0.0


@dataclass(frozen=True)
class ToolQualificationCase:
    name: str
    category: str
    prompt: str
    tool_choice: Any
    expect_tool: bool
    expected_city: str = "Paris"
    expected_text: str = ""
    stream: bool = False
    roundtrip: bool = False
    client_shape: str = ""


TOOL_SPEC: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Return current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


def qualification_log_path() -> Path:
    value = (getattr(S, "MODEL_TOOL_QUALIFICATION_LOG_PATH", "") or "").strip()
    if value:
        return Path(value)
    return Path("/var/lib/gateway/data/model_tool_qualification/results.jsonl")


def _case_timeout_sec() -> float:
    try:
        value = float(getattr(S, "MODEL_TOOL_QUALIFICATION_CASE_TIMEOUT_SEC", 120.0) or 120.0)
    except Exception:
        value = 120.0
    return max(5.0, min(value, 600.0))


def validate_request(req: ModelToolQualificationRequest) -> ModelToolQualificationRequest:
    models = clean_model_list(req.models)
    if not models:
        raise ValueError("at least one model is required")
    if len(models) > MAX_MODELS_PER_RUN:
        raise ValueError(f"at most {MAX_MODELS_PER_RUN} models can be qualified at once")
    if req.max_tokens < 1 or req.max_tokens > MAX_MAX_TOKENS:
        raise ValueError(f"max_tokens must be between 1 and {MAX_MAX_TOKENS}")
    if req.temperature < 0 or req.temperature > 2:
        raise ValueError("temperature must be between 0 and 2")
    return req.model_copy(update={"models": models})


def qualification_cases(req: ModelToolQualificationRequest) -> list[ToolQualificationCase]:
    cases = [
        ToolQualificationCase(
            name="auto_directive_nonstream",
            category="auto",
            prompt=(
                "Call get_weather exactly once for Paris, France. "
                "Do not answer in prose; use the provided tool."
            ),
            tool_choice="auto",
            expect_tool=True,
            expected_city="Paris",
        ),
        ToolQualificationCase(
            name="auto_natural_nonstream",
            category="auto",
            prompt="What is the current weather in Paris? Use the available tool instead of guessing.",
            tool_choice="auto",
            expect_tool=True,
            expected_city="Paris",
        ),
        ToolQualificationCase(
            name="required_nonstream",
            category="required",
            prompt="Call the weather tool for Berlin, Germany.",
            tool_choice="required",
            expect_tool=True,
            expected_city="Berlin",
        ),
        ToolQualificationCase(
            name="named_nonstream",
            category="named",
            prompt="Call get_weather for Tokyo, Japan.",
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            expect_tool=True,
            expected_city="Tokyo",
        ),
        ToolQualificationCase(
            name="none_nonstream",
            category="none",
            prompt="Reply with exactly NO_TOOL_OK. Do not call any tool.",
            tool_choice="none",
            expect_tool=False,
            expected_city="",
            expected_text="NO_TOOL_OK",
        ),
        ToolQualificationCase(
            name="continue_tool_history_nonstream",
            category="client_continue",
            prompt="Continue-style transcript with prior tool result.",
            tool_choice="none",
            expect_tool=False,
            expected_city="",
            expected_text=TOOL_RESULT_MARKER,
            client_shape="continue_tool_history",
        ),
        ToolQualificationCase(
            name="hermes_tool_history_nonstream",
            category="client_hermes",
            prompt="Hermes-style transcript with prior tool result.",
            tool_choice="none",
            expect_tool=False,
            expected_city="",
            expected_text=TOOL_RESULT_MARKER,
            client_shape="hermes_tool_history",
        ),
    ]
    if req.include_stream:
        cases.append(
            ToolQualificationCase(
                name="auto_directive_stream",
                category="stream",
                prompt=(
                    "Call get_weather exactly once for Paris, France. "
                    "Do not answer in prose; use the provided tool."
                ),
                tool_choice="auto",
                expect_tool=True,
                expected_city="Paris",
                stream=True,
            )
        )
    if req.include_roundtrip:
        cases.append(
            ToolQualificationCase(
                name="auto_roundtrip_tool_result",
                category="roundtrip",
                prompt=(
                    "Call get_weather exactly once for Paris, France. "
                    f"After receiving the tool result, include the exact token {TOOL_RESULT_MARKER} in the final answer."
                ),
                tool_choice="auto",
                expect_tool=True,
                expected_city="Paris",
                roundtrip=True,
            )
        )
    return cases


def _base_messages(prompt: str, *, roundtrip: bool = False) -> list[ChatMessage]:
    system = (
        "You are running a Nexus OpenAI-compatible tool-calling qualification. "
        "Use structured tool calls when tools are needed."
    )
    if roundtrip:
        system += f" If a tool result is provided, include the exact token {TOOL_RESULT_MARKER} in the final answer."
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=prompt),
    ]


def _client_shape_messages(case: ToolQualificationCase) -> list[ChatMessage] | None:
    if case.client_shape == "continue_tool_history":
        return [
            ChatMessage(role="system", content="You are validating Continue-style OpenAI-compatible tool history."),
            ChatMessage(
                role="user",
                content=[
                    {
                        "type": "text",
                        "text": (
                            "What is the weather in Paris? Use the available tool. "
                            f"After receiving the result, reply with exactly {TOOL_RESULT_MARKER}."
                        ),
                    }
                ],
            ),
            ChatMessage(
                role="assistant",
                content="",
                toolCalls=[
                    {
                        "id": "Cont1nue1",
                        "function": {"name": TOOL_NAME, "arguments": {"city": "Paris"}},
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                toolCallId="Cont1nue1",
                content=json.dumps({"city": "Paris", "forecast": f"{TOOL_RESULT_MARKER} clear"}, separators=(",", ":")),
            ),
        ]
    if case.client_shape == "hermes_tool_history":
        return [
            ChatMessage(role="system", content="You are validating Hermes-style OpenAI-compatible tool history."),
            ChatMessage(
                role="user",
                content=(
                    "Use a tool to check Paris weather. "
                    f"After receiving the result, reply with exactly {TOOL_RESULT_MARKER}."
                ),
            ),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "Hermes001",
                        "function": {"name": TOOL_NAME, "arguments": {"city": "Paris"}},
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                tool_call_id="Hermes001",
                content={"city": "Paris", "forecast": f"{TOOL_RESULT_MARKER} clear"},
            ),
        ]
    return None


def build_chat_request(
    req: ModelToolQualificationRequest,
    model: str,
    case: ToolQualificationCase,
    *,
    messages: Optional[list[ChatMessage]] = None,
    tool_choice: Any = None,
    stream: Optional[bool] = None,
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=messages or _client_shape_messages(case) or _base_messages(case.prompt, roundtrip=case.roundtrip),
        tools=[TOOL_SPEC],
        tool_choice=case.tool_choice if tool_choice is None else tool_choice,
        parallel_tool_calls=False,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        stream=case.stream if stream is None else stream,
        stream_options={"include_usage": True} if (case.stream if stream is None else stream) else None,
    )


def _message_dicts(messages: Iterable[ChatMessage]) -> list[dict[str, Any]]:
    return [m.model_dump(exclude_none=True) for m in messages]


def _route_chat_request(cc: ChatCompletionRequest) -> tuple[Any, str, str, Optional[str]]:
    from app import openai_routes as openai_compat

    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers={},
        messages=_message_dicts(cc.messages),
        has_tools=True,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )
    registry = get_registry()
    backend_class = registry.resolve_backend_class(route.backend) or route.backend
    alias_name = openai_compat._selected_alias_name(cc.model, route.reason)
    return route, backend_class, route.model, alias_name


def _prepare_request_for_backend(
    cc: ChatCompletionRequest,
    *,
    alias_name: Optional[str],
    backend_class: str,
    model_name: str,
) -> tuple[ChatCompletionRequest, Optional[str], bool]:
    from app import openai_routes as openai_compat

    constrained = openai_compat._apply_alias_constraints(cc, alias_name=alias_name)
    return openai_compat._normalize_chat_request_for_backend(
        constrained,
        alias_name=alias_name,
        backend_class=backend_class,
        model_name=model_name,
        enforce_tool_qualification=False,
    )


def _short_snippet(value: Any, *, limit: int = 260) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _raw_tool_like_snippet(content: Any, tool_name: str = TOOL_NAME) -> str:
    if not isinstance(content, str) or not content.strip():
        return ""
    patterns = [
        re.compile(rf"\b{re.escape(tool_name)}\s*(?:\(|\{{|:)", re.IGNORECASE),
        re.compile(rf'"name"\s*:\s*"{re.escape(tool_name)}"', re.IGNORECASE),
        re.compile(r"<tool_call|</tool_call|\btool_calls?\b", re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(content)
        if match:
            start = max(0, match.start() - 60)
            end = min(len(content), match.end() + 160)
            return _short_snippet(content[start:end])
    return ""


def _first_choice(response: Dict[str, Any]) -> Dict[str, Any]:
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def _message_from_response(response: Dict[str, Any]) -> Dict[str, Any]:
    choice = _first_choice(response)
    message = choice.get("message")
    return message if isinstance(message, dict) else {}


def _tool_calls_from_message(message: Dict[str, Any]) -> list[Dict[str, Any]]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _parse_arguments(value: Any) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if isinstance(value, str):
        if not value.strip():
            return {}, None
        try:
            parsed = json.loads(value)
        except Exception as exc:
            return None, f"function.arguments is not valid JSON: {type(exc).__name__}"
        if not isinstance(parsed, dict):
            return None, "function.arguments JSON is not an object"
        return parsed, None
    if isinstance(value, dict):
        return None, "function.arguments must be an OpenAI-compatible JSON string, not an object"
    return None, "function.arguments is missing or not a string"


def _validate_tool_call(
    tool_call: Dict[str, Any],
    *,
    expected_name: str,
    expected_city: str,
) -> tuple[bool, list[str], Dict[str, Any]]:
    errors: list[str] = []
    function = tool_call.get("function")
    if not isinstance(function, dict):
        errors.append("tool_call.function is missing")
        function = {}

    name = str(function.get("name") or "").strip()
    name_error = tool_call_name_error(name)
    if name_error:
        errors.append(f"{name_error}: {_short_snippet(name) or '<missing>'}")
    elif name != expected_name:
        errors.append(f"tool call name {name or '<missing>'} != {expected_name}")

    args, arg_error = _parse_arguments(function.get("arguments"))
    if arg_error:
        errors.append(arg_error)
        args = None

    if expected_city and isinstance(args, dict):
        city = str(args.get("city") or "").strip().lower()
        if expected_city.lower() not in city:
            errors.append(f"tool arguments city {args.get('city')!r} does not contain {expected_city!r}")

    compact = {
        "id": tool_call.get("id") or "",
        "type": tool_call.get("type") or "",
        "name": name,
        "arguments": args if isinstance(args, dict) else None,
    }
    return not errors, errors, compact


def evaluate_tool_response(
    response: Dict[str, Any],
    case: ToolQualificationCase,
    *,
    stage: str = "assistant",
) -> Dict[str, Any]:
    choice = _first_choice(response)
    message = _message_from_response(response)
    tool_calls = _tool_calls_from_message(message)
    content = message.get("content")
    finish_reason = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else ""
    warnings: list[str] = []

    if case.expect_tool:
        if not tool_calls:
            raw = _raw_tool_like_snippet(content)
            reason = "no structured tool_calls returned"
            if raw:
                reason = "raw tool-like text returned instead of structured tool_calls"
            return {
                "ok": False,
                "error": reason,
                "stage": stage,
                "tool_calls_count": 0,
                "finish_reason": finish_reason,
                "content_snippet": _short_snippet(content),
                "raw_tool_like_snippet": raw,
                "warnings": warnings,
            }

        compact_calls: list[Dict[str, Any]] = []
        all_errors: list[str] = []
        matched = False
        for tool_call in tool_calls:
            ok, errors, compact = _validate_tool_call(
                tool_call,
                expected_name=TOOL_NAME,
                expected_city=case.expected_city,
            )
            compact_calls.append(compact)
            if ok:
                matched = True
            else:
                all_errors.extend(errors)

        if finish_reason and finish_reason != "tool_calls":
            warnings.append(f"finish_reason is {finish_reason!r}, expected 'tool_calls'")

        if not matched:
            return {
                "ok": False,
                "error": "; ".join(all_errors[:4]) or "no valid matching tool call returned",
                "stage": stage,
                "tool_calls_count": len(tool_calls),
                "finish_reason": finish_reason,
                "tool_calls": compact_calls,
                "content_snippet": _short_snippet(content),
                "warnings": warnings,
            }

        return {
            "ok": True,
            "stage": stage,
            "tool_calls_count": len(tool_calls),
            "finish_reason": finish_reason,
            "tool_calls": compact_calls,
            "content_snippet": _short_snippet(content),
            "warnings": warnings,
        }

    raw = _raw_tool_like_snippet(content)
    if tool_calls:
        return {
            "ok": False,
            "error": "tool_calls returned despite tool_choice=none",
            "stage": stage,
            "tool_calls_count": len(tool_calls),
            "finish_reason": finish_reason,
            "content_snippet": _short_snippet(content),
        }
    if raw:
        return {
            "ok": False,
            "error": "raw tool-like text returned despite tool_choice=none",
            "stage": stage,
            "tool_calls_count": 0,
            "finish_reason": finish_reason,
            "content_snippet": _short_snippet(content),
            "raw_tool_like_snippet": raw,
        }
    if case.expected_text:
        content_text = content if isinstance(content, str) else ""
        if case.expected_text not in content_text:
            return {
                "ok": False,
                "error": f"final answer did not include {case.expected_text}",
                "stage": stage,
                "tool_calls_count": 0,
                "finish_reason": finish_reason,
                "content_snippet": _short_snippet(content),
            }
    return {
        "ok": True,
        "stage": stage,
        "tool_calls_count": 0,
        "finish_reason": finish_reason,
        "content_snippet": _short_snippet(content),
    }


def _merge_stream_tool_call(slots: Dict[int, Dict[str, Any]], raw_call: Dict[str, Any]) -> None:
    try:
        index = int(raw_call.get("index", 0) or 0)
    except Exception:
        index = 0
    slot = slots.setdefault(index, {"function": {}})
    for key in ("id", "type"):
        value = raw_call.get(key)
        if isinstance(value, str) and value:
            slot[key] = value
    function = raw_call.get("function")
    if not isinstance(function, dict):
        return
    out_function = slot.setdefault("function", {})
    name = function.get("name")
    if isinstance(name, str) and name:
        existing = str(out_function.get("name") or "")
        out_function["name"] = name if not existing or existing == name else existing + name
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        out_function["arguments"] = str(out_function.get("arguments") or "") + arguments


async def _collect_stream_response(upstream_gen: Any) -> Dict[str, Any]:
    content_parts: list[str] = []
    usage: Dict[str, Any] = {}
    finish_reason = ""
    stream_error = ""
    tool_slots: Dict[int, Dict[str, Any]] = {}
    stream_done = False

    async for chunk in upstream_gen:
        for event in iter_sse_payloads(chunk):
            if event == "[DONE]":
                stream_done = True
                break
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("error"), dict):
                stream_error = json.dumps(event["error"], ensure_ascii=False, separators=(",", ":"), default=str)
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    content_parts.append(content)
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for raw_call in tool_calls:
                        if isinstance(raw_call, dict):
                            _merge_stream_tool_call(tool_slots, raw_call)
        if stream_done:
            break

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    tool_calls = [tool_slots[index] for index in sorted(tool_slots.keys())]
    if tool_calls:
        message["tool_calls"] = tool_calls
    out = {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }
    if stream_error:
        out["stream_error"] = stream_error
    return out


async def _call_case_backend(
    cc: ChatCompletionRequest,
    *,
    backend: str,
    model_name: str,
    timeout_sec: float,
    buffer_tool_stream: bool = False,
) -> Dict[str, Any]:
    if cc.stream:
        if buffer_tool_stream:
            from app.tool_calling.streaming import stream_final_chat_response

            nonstream = cc.model_copy(update={"stream": False, "stream_options": None})
            response = await asyncio.wait_for(
                call_backend_chat(nonstream, backend, model_name),
                timeout=timeout_sec,
            )
            return await asyncio.wait_for(
                _collect_stream_response(stream_final_chat_response(response)),
                timeout=timeout_sec,
            )
        upstream_gen = stream_backend_chat_as_openai(cc, backend, model_name)
        return await asyncio.wait_for(_collect_stream_response(upstream_gen), timeout=timeout_sec)
    return await asyncio.wait_for(call_backend_chat(cc, backend, model_name), timeout=timeout_sec)


async def _run_prepared_request(
    cc: ChatCompletionRequest,
    *,
    case: ToolQualificationCase,
    model: str,
    run_id: str,
    stage: str = "assistant",
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    started = time.monotonic()
    created_at = now_unix()
    route_reason = ""
    backend = ""
    backend_class = ""
    upstream_model = ""
    alias_name: Optional[str] = None
    admission_acquired = False
    admission = get_admission_controller()

    base: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "model": model,
        "case": case.name,
        "category": case.category,
        "stream": bool(cc.stream),
        "tool_choice": cc.tool_choice,
        "expect_tool": case.expect_tool,
        "stage": stage,
    }

    try:
        route, backend_class, upstream_model, alias_name = _route_chat_request(cc)
        backend = route.backend
        route_reason = route.reason
        native_tools = backend_supports_tool_calling(backend_class)

        base.update(
            {
                "backend": backend,
                "backend_class": backend_class,
                "resolved_model": upstream_model,
                "router_reason": route_reason,
                "selected_alias": alias_name,
                "backend_native_tools": native_tools,
            }
        )

        check_backend_ready(backend_class, route_kind="chat")
        await check_capability(backend_class, "chat")
        await admission.acquire(backend_class, "chat")
        admission_acquired = True

        normalized_cc, degradation_reason, tools_required_error = _prepare_request_for_backend(
            cc,
            alias_name=alias_name,
            backend_class=backend_class,
            model_name=upstream_model,
        )
        base["degraded_tools"] = bool(degradation_reason)
        if degradation_reason:
            base["degradation_reason"] = degradation_reason
        if tools_required_error:
            result = {
                **base,
                "ok": False,
                "error": f"tools were explicitly required, but {degradation_reason}",
                "completed_at": now_unix(),
                "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
            return result, {}
        if case.expect_tool and degradation_reason and not normalized_cc.tools:
            result = {
                **base,
                "ok": False,
                "error": f"tool fields were stripped before upstream call: {degradation_reason}",
                "completed_at": now_unix(),
                "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
            return result, {}

        buffer_tool_stream = False
        if normalized_cc.stream and alias_name:
            from app import openai_routes as openai_compat

            alias_config = openai_compat.get_aliases().get(alias_name)
            buffer_tool_stream = bool(
                alias_config is not None
                and getattr(alias_config, "buffer_tool_call_stream", False)
                and normalized_cc.tools
            )
        base["buffered_tool_stream"] = buffer_tool_stream

        response = await _call_case_backend(
            normalized_cc,
            backend=backend,
            model_name=upstream_model,
            timeout_sec=_case_timeout_sec(),
            buffer_tool_stream=buffer_tool_stream,
        )
        if response.get("stream_error"):
            result = {
                **base,
                "ok": False,
                "error": str(response.get("stream_error") or "")[:1200],
                "completed_at": now_unix(),
                "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
            return result, response

        evaluation = evaluate_tool_response(response, case, stage=stage)
        result = {
            **base,
            **evaluation,
            "completed_at": now_unix(),
            "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
        return result, response
    except Exception as exc:
        result = {
            **base,
            "ok": False,
            "error": error_text(exc)[:4000],
            "backend": backend,
            "backend_class": backend_class,
            "resolved_model": upstream_model,
            "router_reason": route_reason,
            "selected_alias": alias_name,
            "completed_at": now_unix(),
            "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
        return result, {}
    finally:
        if admission_acquired:
            admission.release(backend_class, "chat")


async def run_case(
    req: ModelToolQualificationRequest,
    *,
    model: str,
    case: ToolQualificationCase,
    run_id: str,
) -> Dict[str, Any]:
    if not case.roundtrip:
        cc = build_chat_request(req, model, case)
        result, _response = await _run_prepared_request(cc, case=case, model=model, run_id=run_id)
        return result

    first_cc = build_chat_request(req, model, case, stream=False)
    first_result, first_response = await _run_prepared_request(
        first_cc,
        case=case,
        model=model,
        run_id=run_id,
        stage="tool_call",
    )
    if not first_result.get("ok"):
        return {
            **first_result,
            "ok": False,
            "stage": "roundtrip",
            "error": f"roundtrip first turn failed: {first_result.get('error') or 'tool call failed'}",
        }

    first_message = _message_from_response(first_response)
    tool_calls = _tool_calls_from_message(first_message)
    tool_call_id = str((tool_calls[0] if tool_calls else {}).get("id") or "call_qualification")
    messages = _base_messages(case.prompt, roundtrip=True)
    messages.append(ChatMessage(role="assistant", content=None, tool_calls=tool_calls))
    messages.append(
        ChatMessage(
            role="tool",
            tool_call_id=tool_call_id,
            content=json.dumps({"city": "Paris", "forecast": f"{TOOL_RESULT_MARKER} clear"}, separators=(",", ":")),
        )
    )
    final_cc = build_chat_request(req, model, case, messages=messages, tool_choice="none", stream=False)
    final_case = ToolQualificationCase(
        name=case.name,
        category=case.category,
        prompt=case.prompt,
        tool_choice="none",
        expect_tool=False,
        expected_city="",
        roundtrip=True,
    )
    final_result, final_response = await _run_prepared_request(
        final_cc,
        case=final_case,
        model=model,
        run_id=run_id,
        stage="tool_result",
    )
    if not final_result.get("ok"):
        return {
            **final_result,
            "stage": "roundtrip",
            "error": f"roundtrip final turn failed: {final_result.get('error') or 'final response failed'}",
        }

    final_content = str(_message_from_response(final_response).get("content") or "")
    if TOOL_RESULT_MARKER not in final_content:
        return {
            **final_result,
            "ok": False,
            "stage": "roundtrip",
            "error": f"roundtrip final answer did not include {TOOL_RESULT_MARKER}",
            "content_snippet": _short_snippet(final_content),
        }

    return {
        **final_result,
        "ok": True,
        "stage": "roundtrip",
        "tool_calls_count": int(first_result.get("tool_calls_count") or 0),
        "first_turn_finish_reason": first_result.get("finish_reason") or "",
    }


def summarize_model_results(results: list[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    passed = len([item for item in results if item.get("ok") is True])
    by_category: Dict[str, Dict[str, int]] = OrderedDict()
    first_error = ""
    for item in results:
        category = str(item.get("category") or "unknown")
        bucket = by_category.setdefault(category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if item.get("ok") is True:
            bucket["passed"] += 1
        elif not first_error:
            first_error = str(item.get("error") or "")[:400]

    return {
        "passed": passed,
        "total": total,
        "ok": bool(total and passed == total),
        "by_category": by_category,
        "first_error": first_error,
    }


async def run_model_qualification(
    req: ModelToolQualificationRequest,
    *,
    model: str,
    run_id: str,
) -> Dict[str, Any]:
    started = time.monotonic()
    created_at = now_unix()
    results: list[Dict[str, Any]] = []
    for case in qualification_cases(req):
        results.append(await run_case(req, model=model, case=case, run_id=run_id))

    summary = summarize_model_results(results)
    first_route = next((item for item in results if item.get("backend") or item.get("resolved_model")), {})
    return {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "completed_at": now_unix(),
        "model": model,
        "ok": summary["ok"],
        "backend": first_route.get("backend") or first_route.get("backend_class") or "",
        "backend_class": first_route.get("backend_class") or "",
        "resolved_model": first_route.get("resolved_model") or "",
        "router_reason": first_route.get("router_reason") or "",
        "selected_alias": first_route.get("selected_alias") or "",
        "backend_native_tools": first_route.get("backend_native_tools"),
        "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
        "summary": summary,
        "cases": results,
    }


def append_result(result: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    target = path or qualification_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(result, separators=(",", ":"), sort_keys=True, default=str))
            handle.write("\n")
    except Exception:
        return


async def run_qualification(req: ModelToolQualificationRequest) -> Dict[str, Any]:
    req = validate_request(req)
    if _QUALIFICATION_LOCK.locked():
        raise ToolQualificationBusy("another tool qualification run is already running")

    async with _QUALIFICATION_LOCK:
        run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        results: list[Dict[str, Any]] = []
        for model in req.models:
            result = await run_model_qualification(req, model=model, run_id=run_id)
            append_result(result)
            results.append(result)

        return {
            "ok": True,
            "run_id": run_id,
            "generated_at": now_unix(),
            "settings": {
                "models": req.models,
                "include_stream": req.include_stream,
                "include_roundtrip": req.include_roundtrip,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
            },
            "summary": summarize(results),
            "results": results,
            "recent": recent_runs(limit=5),
        }


def _read_recent_result_lines(path: Path, *, max_lines: int = RECENT_HISTORY_LINES) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except Exception:
        return []

    out: list[Dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("schema") == SCHEMA_VERSION:
            out.append(item)
    return out


def summarize(results: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for item in results:
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        by_category = summary.get("by_category") if isinstance(summary.get("by_category"), dict) else {}
        rows.append(
            {
                "model": item.get("model") or "",
                "ok": bool(item.get("ok") is True),
                "passed": summary.get("passed", 0),
                "total": summary.get("total", 0),
                "auto": by_category.get("auto", {}),
                "required": by_category.get("required", {}),
                "named": by_category.get("named", {}),
                "stream": by_category.get("stream", {}),
                "roundtrip": by_category.get("roundtrip", {}),
                "none": by_category.get("none", {}),
                "client_continue": by_category.get("client_continue", {}),
                "client_hermes": by_category.get("client_hermes", {}),
                "backend": item.get("backend") or item.get("backend_class") or "",
                "resolved_model": item.get("resolved_model") or "",
                "error": summary.get("first_error") or "",
            }
        )
    return rows


def recent_runs(*, limit: int = 5, path: Optional[Path] = None) -> list[Dict[str, Any]]:
    cap = max(1, min(int(limit or 5), 20))
    items = _read_recent_result_lines(path or qualification_log_path())
    by_run: OrderedDict[str, list[Dict[str, Any]]] = OrderedDict()
    for item in items:
        run_id = str(item.get("run_id") or "").strip()
        if not run_id:
            continue
        by_run.setdefault(run_id, []).append(item)

    runs: list[Dict[str, Any]] = []
    for run_id, results in by_run.items():
        completed = max([float(item.get("completed_at") or 0) for item in results] or [0.0])
        runs.append(
            {
                "run_id": run_id,
                "completed_at": completed,
                "models": clean_model_list([str(item.get("model") or "") for item in results]),
                "summary": summarize(results),
            }
        )
    runs.sort(key=lambda item: float(item.get("completed_at") or 0), reverse=True)
    return runs[:cap]


def _compact_result(item: Dict[str, Any]) -> Dict[str, Any]:
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    by_category = summary.get("by_category") if isinstance(summary.get("by_category"), dict) else {}
    return {
        "model": item.get("model") or "",
        "run_id": item.get("run_id") or "",
        "completed_at": item.get("completed_at") or item.get("created_at") or 0,
        "ok": bool(item.get("ok") is True),
        "passed": summary.get("passed", 0),
        "total": summary.get("total", 0),
        "by_category": by_category,
        "backend": item.get("backend") or item.get("backend_class") or "",
        "backend_class": item.get("backend_class") or "",
        "resolved_model": item.get("resolved_model") or "",
        "backend_native_tools": item.get("backend_native_tools"),
        "first_error": summary.get("first_error") or "",
    }


def latest_by_model(*, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    items = _read_recent_result_lines(path or qualification_log_path())
    latest: Dict[str, Dict[str, Any]] = {}
    latest_time: Dict[str, float] = {}
    for item in items:
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        completed = float(item.get("completed_at") or item.get("created_at") or 0)
        keys = [model]
        backend = str(item.get("backend") or item.get("backend_class") or "").strip()
        backend_class = str(item.get("backend_class") or "").strip()
        resolved_model = str(item.get("resolved_model") or "").strip()
        if backend and resolved_model:
            keys.append(f"{backend}:{resolved_model}")
        if backend_class and backend_class != backend and resolved_model:
            keys.append(f"{backend_class}:{resolved_model}")
        compact = _compact_result(item)
        for key in keys:
            if key not in latest_time or completed >= latest_time[key]:
                latest_time[key] = completed
                latest[key] = compact
    return latest


def _max_age_sec() -> int:
    try:
        value = int(getattr(S, "MODEL_TOOL_QUALIFICATION_MAX_AGE_SEC", 0) or 0)
    except Exception:
        value = 0
    return max(0, value)


def _tool_choice_category(tool_choice: Any, *, has_tools: bool = True) -> str:
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        if normalized == "none":
            return "none"
        if normalized == "required":
            return "required"
        return "auto"
    if isinstance(tool_choice, dict):
        normalized = str(tool_choice.get("type") or "").strip().lower()
        if normalized == "none":
            return "none"
        if normalized in {"required", "function"}:
            return "named" if normalized == "function" else "required"
        return "auto"
    return "auto" if has_tools else "none"


def _category_passed(result: Dict[str, Any], category: str) -> bool:
    if category in {"", "none"}:
        return True
    by_category = result.get("by_category")
    if not isinstance(by_category, dict):
        return bool(result.get("ok") is True)
    bucket = by_category.get(category)
    if not isinstance(bucket, dict):
        return bool(result.get("ok") is True)
    try:
        total = int(bucket.get("total") or 0)
        passed = int(bucket.get("passed") or 0)
    except Exception:
        return False
    return total > 0 and passed == total


def _expected_categories(*, include_stream: bool = True, include_roundtrip: bool = True) -> list[str]:
    categories = ["auto", "required", "named", "none", "client_continue", "client_hermes"]
    if include_stream:
        categories.append("stream")
    if include_roundtrip:
        categories.append("roundtrip")
    return categories


def _missing_expected_categories(
    result: Optional[Dict[str, Any]],
    *,
    include_stream: bool = True,
    include_roundtrip: bool = True,
) -> list[str]:
    if not result:
        return _expected_categories(include_stream=include_stream, include_roundtrip=include_roundtrip)
    by_category = result.get("by_category")
    if not isinstance(by_category, dict):
        return _expected_categories(include_stream=include_stream, include_roundtrip=include_roundtrip)

    missing: list[str] = []
    for category in _expected_categories(include_stream=include_stream, include_roundtrip=include_roundtrip):
        bucket = by_category.get(category)
        if not isinstance(bucket, dict):
            missing.append(category)
            continue
        try:
            total = int(bucket.get("total") or 0)
        except Exception:
            total = 0
        if total <= 0:
            missing.append(category)
    return missing


def _result_matches_target(result: Dict[str, Any], *, backend_class: str, resolved_model: str) -> bool:
    result_backend = str(result.get("backend") or "").strip()
    result_backend_class = str(result.get("backend_class") or "").strip()
    result_model = str(result.get("resolved_model") or "").strip()
    if not result_backend and not result_model:
        return True
    return result_model == resolved_model and backend_class in {result_backend, result_backend_class}


def qualification_status_for_target(
    *,
    alias_name: Optional[str],
    backend_class: str,
    resolved_model: str,
    tool_choice: Any = "auto",
    has_tools: bool = True,
    path: Optional[Path] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    latest = latest_by_model(path=path)
    backend_key = f"{backend_class}:{resolved_model}" if backend_class and resolved_model else ""
    alias_key = (alias_name or "").strip()
    backend_result = latest.get(backend_key) if backend_key else None
    alias_result = latest.get(alias_key) if alias_key else None
    result = backend_result or alias_result
    category = _tool_choice_category(tool_choice, has_tools=has_tools)

    if category == "none":
        return {"qualified": True, "category": category, "key": backend_key or alias_key, "result": result}

    if result is None:
        qualified = not bool(getattr(S, "MODEL_TOOL_QUALIFICATION_GUARDRAIL_REQUIRE_RESULT", False))
        return {
            "qualified": qualified,
            "category": category,
            "key": backend_key or alias_key,
            "result": None,
            "reason": "no tool qualification result for selected model",
            "missing": True,
        }

    if not _result_matches_target(result, backend_class=backend_class, resolved_model=resolved_model):
        return {
            "qualified": False,
            "category": category,
            "key": backend_key or alias_key,
            "result": result,
            "reason": (
                "tool qualification target mismatch: "
                f"latest result is for {result.get('backend') or '-'}:{result.get('resolved_model') or '-'}"
            ),
            "mismatch": True,
        }

    completed_at = int(float(result.get("completed_at") or 0))
    age_sec = max(0, int((now_unix() if now is None else now) - completed_at)) if completed_at else 0
    max_age = _max_age_sec()
    if max_age and completed_at and age_sec > max_age:
        return {
            "qualified": False,
            "category": category,
            "key": backend_key or alias_key,
            "result": result,
            "reason": f"tool qualification is stale ({age_sec}s old, max {max_age}s)",
            "stale": True,
            "age_sec": age_sec,
        }

    if not _category_passed(result, category):
        reason = str(result.get("first_error") or f"latest tool qualification did not pass {category} tool calls")
        return {
            "qualified": False,
            "category": category,
            "key": backend_key or alias_key,
            "result": result,
            "reason": reason,
            "failed": True,
            "age_sec": age_sec,
        }

    return {
        "qualified": True,
        "category": category,
        "key": backend_key or alias_key,
        "result": result,
        "age_sec": age_sec,
    }


def guardrail_reason_for_target(
    *,
    alias_name: Optional[str],
    backend_class: str,
    resolved_model: str,
    tool_choice: Any,
    has_tools: bool = True,
) -> Optional[str]:
    if not bool(getattr(S, "MODEL_TOOL_QUALIFICATION_GUARDRAIL_ENABLED", True)):
        return None
    status = qualification_status_for_target(
        alias_name=alias_name,
        backend_class=backend_class,
        resolved_model=resolved_model,
        tool_choice=tool_choice,
        has_tools=has_tools,
    )
    if status.get("qualified"):
        return None
    require_result = bool(getattr(S, "MODEL_TOOL_QUALIFICATION_GUARDRAIL_REQUIRE_RESULT", False))
    if not require_result:
        if status.get("missing"):
            return None
        # Fail open for an expired result that previously passed the complete
        # suite. This matches the configured missing-result policy and avoids
        # stripping tools while the background scheduler refreshes a healthy,
        # unchanged backend/model target after a gateway restart.
        result = status.get("result")
        if status.get("stale") and isinstance(result, dict) and result.get("ok") is True:
            return None
    return str(status.get("reason") or "latest tool qualification does not allow tool use")


def _auto_run_models() -> list[str]:
    raw = str(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_MODELS", "") or "").strip()
    if raw:
        return clean_model_list(raw.split(","))
    try:
        from app.model_aliases import get_aliases

        aliases = get_aliases()
        return clean_model_list([name for name, alias in aliases.items() if alias.tools is not False])
    except Exception:
        return []


async def auto_qualification_candidates(models: Optional[list[str]] = None) -> list[str]:
    candidates: list[str] = []
    include_stream = bool(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_INCLUDE_STREAM", True))
    include_roundtrip = bool(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_INCLUDE_ROUNDTRIP", True))
    for model in clean_model_list(models or _auto_run_models()):
        cc = build_chat_request(
            ModelToolQualificationRequest(models=[model], include_stream=False, include_roundtrip=False),
            model,
            qualification_cases(ModelToolQualificationRequest(models=[model], include_stream=False, include_roundtrip=False))[0],
        )
        try:
            route, backend_class, upstream_model, alias_name = _route_chat_request(cc)
            status = qualification_status_for_target(
                alias_name=alias_name,
                backend_class=backend_class,
                resolved_model=upstream_model,
                tool_choice="auto",
                has_tools=True,
            )
        except Exception as exc:
            logger.info("tool qualification auto-run: skipping model=%s route failed (%s: %s)", model, type(exc).__name__, exc)
            continue
        missing_categories = _missing_expected_categories(
            status.get("result") if isinstance(status.get("result"), dict) else None,
            include_stream=include_stream,
            include_roundtrip=include_roundtrip,
        )
        result = status.get("result") if isinstance(status.get("result"), dict) else None
        suite_failed = result is not None and result.get("ok") is not True
        if (
            status.get("missing")
            or status.get("mismatch")
            or status.get("stale")
            or status.get("failed")
            or suite_failed
            or missing_categories
        ):
            if missing_categories and not status.get("missing"):
                logger.info(
                    "tool qualification auto-run: model=%s missing suite categories=%s",
                    model,
                    ",".join(missing_categories),
                )
            elif suite_failed:
                logger.info("tool qualification auto-run: model=%s latest full suite failed", model)
            candidates.append(model)
    return clean_model_list(candidates)


async def run_auto_qualification_once(*, reason: str = "scheduled") -> Optional[Dict[str, Any]]:
    models = await auto_qualification_candidates()
    if not models:
        logger.info("tool qualification auto-run: no stale or missing configured models reason=%s", reason)
        return None
    if _QUALIFICATION_LOCK.locked():
        logger.info("tool qualification auto-run: skipped because a run is already active reason=%s models=%s", reason, ",".join(models))
        return None

    logger.info("tool qualification auto-run: starting reason=%s models=%s", reason, ",".join(models))
    req = ModelToolQualificationRequest(
        models=models,
        include_stream=bool(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_INCLUDE_STREAM", True)),
        include_roundtrip=bool(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_INCLUDE_ROUNDTRIP", True)),
    )
    return await run_qualification(req)


async def _scheduler_loop(stop_event: asyncio.Event) -> None:
    delay = max(0.0, float(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_DELAY_SEC", 45.0) or 0.0))
    interval = max(0.0, float(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_INTERVAL_SEC", 60 * 60 * 24) or 0.0))
    first_run = True
    try:
        if delay:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
        while not stop_event.is_set():
            try:
                await run_auto_qualification_once(reason="startup" if first_run else "scheduled")
            except Exception as exc:
                logger.warning("tool qualification auto-run failed (%s: %s)", type(exc).__name__, exc)
            first_run = False
            if interval <= 0:
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        return


async def start_scheduler() -> None:
    global _SCHEDULER_TASK, _SCHEDULER_STOP
    if not bool(getattr(S, "MODEL_TOOL_QUALIFICATION_AUTO_RUN_ENABLED", True)):
        return
    if _SCHEDULER_TASK is not None and not _SCHEDULER_TASK.done():
        return
    _SCHEDULER_STOP = asyncio.Event()
    _SCHEDULER_TASK = asyncio.create_task(_scheduler_loop(_SCHEDULER_STOP))


async def stop_scheduler() -> None:
    global _SCHEDULER_TASK, _SCHEDULER_STOP
    if _SCHEDULER_STOP is not None:
        _SCHEDULER_STOP.set()
    task = _SCHEDULER_TASK
    _SCHEDULER_TASK = None
    _SCHEDULER_STOP = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
