from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from app.config import S
from app.models import ChatCompletionRequest, ChatMessage
from app.model_aliases import ModelAlias
from app.openai_utils import new_id, now_unix, normalize_tool_calls_for_openai
from app.tool_calling.registry import builtin_tool_definitions, enabled_tool_names, openai_tools_for_policy, redact_secrets
from app.tool_calling.schemas import ToolSchemaError, normalize_tool_definition, validate_arguments


log = logging.getLogger("uvicorn.error")
ExecutionMode = Literal["gateway_exec", "client_exec", "disabled"]


@dataclass(frozen=True)
class NexusToolExecutionPolicy:
    mode: ExecutionMode
    toolsets: frozenset[str]
    client_tool_policy: str
    max_tool_rounds: int
    max_parallel_tools: int
    per_tool_timeout_sec: float
    loop_timeout_sec: float
    output_limit: int
    stream_tool_events: bool = False


@dataclass(frozen=True)
class GatewayToolLoopResult:
    response: dict[str, Any]
    calls_seen: tuple[str, ...]
    tools_executed: tuple[str, ...]
    rounds: int
    stopped_reason: str


def _csv(value: str) -> set[str]:
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def resolve_execution_policy(req: ChatCompletionRequest, alias: ModelAlias | None) -> NexusToolExecutionPolicy:
    extension = req.x_nexus if isinstance(req.x_nexus, dict) else {}
    mode = str(extension.get("tool_execution_mode") or S.NEXUS_TOOL_EXECUTION_DEFAULT).strip().lower()
    if mode not in {"gateway_exec", "client_exec", "disabled"}:
        raise ValueError("x_nexus.tool_execution_mode must be gateway_exec, client_exec, or disabled")
    configured_toolsets = extension.get("toolsets")
    if isinstance(configured_toolsets, list):
        toolsets = {str(item).strip() for item in configured_toolsets if str(item).strip()}
    elif alias and getattr(alias, "toolsets", ()):
        toolsets = set(getattr(alias, "toolsets", ()))
    else:
        toolsets = _csv(S.NEXUS_AUTO_INJECT_TOOLSETS)
    client_policy = str(extension.get("client_tools") or S.NEXUS_CLIENT_TOOL_POLICY).strip().lower()
    if client_policy not in {"replace", "merge", "client"}:
        raise ValueError("x_nexus.client_tools must be replace, merge, or client")
    max_rounds = min(max(int(extension.get("max_tool_rounds") or (getattr(alias, "max_tool_rounds", None) if alias else 0) or S.NEXUS_TOOL_MAX_ROUNDS), 1), 16)
    max_parallel = min(max(int(extension.get("max_parallel_tools") or S.NEXUS_TOOL_MAX_PARALLEL), 1), 16)
    return NexusToolExecutionPolicy(
        mode=mode,  # type: ignore[arg-type]
        toolsets=frozenset(toolsets),
        client_tool_policy=client_policy,
        max_tool_rounds=max_rounds,
        max_parallel_tools=max_parallel,
        per_tool_timeout_sec=max(0.1, float(S.NEXUS_TOOL_TIMEOUT_SEC)),
        loop_timeout_sec=max(1.0, float(S.NEXUS_TOOL_LOOP_TIMEOUT_SEC)),
        output_limit=max(1000, int(S.NEXUS_TOOL_OUTPUT_MAX_CHARS)),
        stream_tool_events=extension.get("stream_tool_events") is True,
    )


def prepare_tools(req: ChatCompletionRequest, policy: NexusToolExecutionPolicy, alias: ModelAlias | None) -> ChatCompletionRequest:
    if policy.mode == "disabled":
        if req.tools or req.tool_choice is not None:
            raise ValueError("tool use is disabled for this request")
        return req
    if policy.mode != "gateway_exec":
        return req
    if alias is not None and alias.tools is False:
        raise ValueError(f"model alias '{req.model}' does not support tool calling")

    gateway_tools = openai_tools_for_policy(set(policy.toolsets)) if (S.NEXUS_AUTO_INJECT_TOOLS or (alias and getattr(alias, "auto_inject_tools", False))) else []
    client_tools = [
        normalize_tool_definition(tool.model_dump(exclude_none=True) if hasattr(tool, "model_dump") else tool, mode="best_effort")
        for tool in (req.tools or [])
    ]
    if policy.client_tool_policy == "client":
        selected = client_tools
    elif policy.client_tool_policy == "merge":
        by_name = {tool["function"]["name"]: tool for tool in gateway_tools}
        for tool in client_tools:
            by_name.setdefault(tool["function"]["name"], tool)
        selected = list(by_name.values())
    else:
        selected = gateway_tools
    if not selected:
        raise ValueError("gateway_exec requires at least one approved tool")
    if isinstance(req.tool_choice, dict):
        function = req.tool_choice.get("function")
        named = str((function or {}).get("name") or "").strip() if isinstance(function, dict) else ""
        available = {tool["function"]["name"] for tool in selected}
        if named and named not in available:
            raise ValueError(f"named tool '{named}' is not approved for gateway_exec")
    normalized = [normalize_tool_definition(tool, mode="strict_autofix") for tool in selected]
    return req.model_copy(
        update={
            "tools": normalized,
            "tool_choice": req.tool_choice if req.tool_choice is not None else "auto",
            "parallel_tool_calls": req.parallel_tool_calls if req.parallel_tool_calls is not None else True,
            "stream": False,
        }
    )


def _bounded_json(value: Any, limit: int) -> str:
    text = json.dumps(redact_secrets(value), ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= limit:
        return text
    return json.dumps({"ok": False, "error": "tool_output_truncated", "preview": text[: max(1, limit - 100)]}, ensure_ascii=False)


def _audit(event: dict[str, Any]) -> None:
    path = Path(S.NEXUS_TOOL_AUDIT_PATH)
    safe = redact_secrets(event)
    safe.pop("arguments", None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=True, default=str) + "\n")
    except OSError:
        log.warning("gateway tool audit write failed path=%s", path)


async def _execute_one(call: dict[str, Any], policy: NexusToolExecutionPolicy, allowed: set[str], request_id: str) -> tuple[str, str, dict[str, Any]]:
    function = call.get("function") if isinstance(call, dict) else None
    name = str((function or {}).get("name") or "").strip()
    call_id = str(call.get("id") or new_id("call"))
    definitions = builtin_tool_definitions()
    definition = definitions.get(name)
    started = time.monotonic()
    if not name or definition is None:
        result = {"ok": False, "error": "unknown_tool", "tool": name}
    elif name not in allowed or definition.implementation is None:
        result = {"ok": False, "error": "tool_not_authorized", "tool": name}
    else:
        raw = (function or {}).get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            arguments = None
        issues = validate_arguments(definition.parameters, arguments)
        if issues:
            result = {"ok": False, "error": "invalid_arguments", "issues": issues}
        else:
            try:
                result = await asyncio.wait_for(definition.implementation(arguments), timeout=min(policy.per_tool_timeout_sec, definition.timeout_sec))
            except asyncio.TimeoutError:
                result = {"ok": False, "error": "tool_timeout"}
            except Exception as exc:
                result = {"ok": False, "error": "tool_failed", "detail": f"{type(exc).__name__}: {exc}"}
    _audit({
        "ts": now_unix(),
        "request_id": request_id,
        "tool_call_id": call_id,
        "tool": name,
        "ok": result.get("ok") is True,
        "error": result.get("error"),
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    })
    return call_id, name, result


def _max_rounds_response(model: str, rounds: int) -> dict[str, Any]:
    return {
        "id": new_id("chatcmpl"),
        "object": "chat.completion",
        "created": now_unix(),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"Nexus stopped tool execution after {rounds} rounds."}, "finish_reason": "stop"}],
    }


async def run_gateway_tool_loop(
    initial_req: ChatCompletionRequest,
    *,
    policy: NexusToolExecutionPolicy,
    alias: ModelAlias | None,
    call_backend: Callable[[ChatCompletionRequest], Awaitable[dict[str, Any]]],
    request_id: str,
) -> GatewayToolLoopResult:
    req = prepare_tools(initial_req, policy, alias)
    allowed = enabled_tool_names(set(policy.toolsets))
    calls_seen: list[str] = []
    executed: list[str] = []

    async def loop() -> GatewayToolLoopResult:
        nonlocal req
        for round_index in range(policy.max_tool_rounds + 1):
            response = await call_backend(req)
            choice = ((response.get("choices") or [{}])[0] if isinstance(response, dict) else {}) or {}
            message = choice.get("message") if isinstance(choice, dict) else {}
            tool_calls = normalize_tool_calls_for_openai((message or {}).get("tool_calls"), generate_missing_ids=True)
            if not isinstance(tool_calls, list) or not tool_calls:
                return GatewayToolLoopResult(response, tuple(calls_seen), tuple(executed), round_index, "final_answer")
            if round_index >= policy.max_tool_rounds:
                stopped = _max_rounds_response(str(response.get("model") or req.model), policy.max_tool_rounds)
                return GatewayToolLoopResult(stopped, tuple(calls_seen), tuple(executed), round_index, "max_tool_rounds")

            assistant = dict(message or {})
            assistant["role"] = "assistant"
            assistant["content"] = assistant.get("content")
            assistant["tool_calls"] = tool_calls
            messages = [*req.messages, ChatMessage(**assistant)]
            semaphore = asyncio.Semaphore(policy.max_parallel_tools)

            async def guarded(call: dict[str, Any]):
                async with semaphore:
                    return await _execute_one(call, policy, allowed, request_id)

            results = await asyncio.gather(*(guarded(call) for call in tool_calls))
            for call_id, name, result in results:
                calls_seen.append(name)
                if name in allowed:
                    executed.append(name)
                messages.append(ChatMessage(role="tool", tool_call_id=call_id, content=_bounded_json(result, policy.output_limit)))
            req = req.model_copy(update={"messages": messages, "tool_choice": "auto", "stream": False})
        raise AssertionError("unreachable")

    try:
        result = await asyncio.wait_for(loop(), timeout=policy.loop_timeout_sec)
    except asyncio.TimeoutError:
        response = _max_rounds_response(req.model, len(calls_seen))
        result = GatewayToolLoopResult(response, tuple(calls_seen), tuple(executed), len(calls_seen), "loop_timeout")
    log.info(
        "gateway tool loop request_id=%s model=%s rounds=%s calls=%s executed=%s stop=%s",
        request_id,
        req.model,
        result.rounds,
        list(result.calls_seen),
        list(result.tools_executed),
        result.stopped_reason,
    )
    return result
