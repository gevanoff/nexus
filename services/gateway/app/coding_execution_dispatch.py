from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException

from app import coding_backend_failover
from app import coding_execution_policy


def _message_role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or "").strip()
    return str(getattr(message, "role", "") or "").strip()


def _message_content(message: Any) -> str:
    if isinstance(message, Mapping):
        value = message.get("content")
    else:
        value = getattr(message, "content", None)
    return value if isinstance(value, str) else ""


def _message_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, Mapping):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    return list(calls) if isinstance(calls, list) else []


def _copy_message(message: Any, **updates: Any) -> Any:
    copier = getattr(message, "model_copy", None)
    if callable(copier):
        return copier(update=updates)
    copier = getattr(message, "copy", None)
    if callable(copier):
        return copier(update=updates)
    payload = dict(message) if isinstance(message, Mapping) else dict(vars(message))
    payload.update(updates)
    return type(message)(**payload)


def _copy_request(req: Any, **updates: Any) -> Any:
    copier = getattr(req, "model_copy", None)
    if callable(copier):
        return copier(update=updates)
    copier = getattr(req, "copy", None)
    if callable(copier):
        return copier(update=updates)
    payload = dict(vars(req))
    payload.update(updates)
    return type(req)(**payload)


def _tool_call_parts(call: Any) -> tuple[str, str, Any]:
    raw = call.model_dump() if hasattr(call, "model_dump") else call
    if not isinstance(raw, Mapping):
        return "", "", {}
    call_id = str(raw.get("id") or "").strip()
    fn = raw.get("function") if isinstance(raw.get("function"), Mapping) else {}
    name = str(fn.get("name") or "").strip()
    arguments = fn.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    return call_id, name, arguments


def _is_coding_execution_request(req: Any) -> bool:
    for message in list(getattr(req, "messages", None) or [])[:3]:
        if _message_role(message) != "system":
            continue
        if "You are Nexus Coding Agent" in _message_content(message):
            return True
    return False


def _clip_text_tool_result(agent: Any, content: str) -> tuple[str, bool]:
    limit = 1000
    configured_limit = getattr(agent, "_tool_context_char_limit", None)
    if callable(configured_limit):
        try:
            limit = min(limit, max(128, int(configured_limit())))
        except Exception:
            limit = 1000
    clip = getattr(agent, "_clip_text", None)
    if callable(clip):
        clipped = str(clip(content, limit))
    else:
        clipped = content if len(content) <= limit else content[: max(0, limit - 1)] + "…"
    return clipped, clipped != content


def _normalize_messages(
    agent: Any,
    messages: list[Any],
    *,
    text_tool_mode: bool,
    fresh_system: str,
) -> tuple[list[Any], dict[str, int]]:
    normalized: list[Any] = []
    tool_names: dict[str, str] = {}
    replaced_system = False
    removed_empty_assistant = 0
    converted_tool_calls = 0
    converted_tool_results = 0
    clipped_tool_results = 0

    for message in messages:
        role = _message_role(message)
        content = _message_content(message)
        calls = _message_tool_calls(message)

        if role == "system" and not replaced_system:
            normalized.append(_copy_message(message, content=fresh_system))
            replaced_system = True
            continue

        if role == "assistant":
            if calls:
                for call in calls:
                    call_id, name, _arguments = _tool_call_parts(call)
                    if call_id and name:
                        tool_names[call_id] = name
            if text_tool_mode and calls:
                blocks = []
                for call in calls:
                    _call_id, name, arguments = _tool_call_parts(call)
                    if not name:
                        continue
                    blocks.append(
                        "<tool_call>"
                        + json.dumps(
                            {"name": name, "arguments": arguments},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "</tool_call>"
                    )
                combined = "\n".join(
                    part for part in [content.strip(), *blocks] if part
                )
                if not combined:
                    removed_empty_assistant += 1
                    continue
                normalized.append(
                    _copy_message(
                        message,
                        content=combined,
                        tool_calls=None,
                        tool_call_id=None,
                    )
                )
                converted_tool_calls += len(blocks)
                continue
            if not content.strip() and not calls:
                removed_empty_assistant += 1
                continue
            normalized.append(message)
            continue

        if role == "tool" and text_tool_mode:
            tool_call_id = (
                str(message.get("tool_call_id") or "").strip()
                if isinstance(message, Mapping)
                else str(getattr(message, "tool_call_id", "") or "").strip()
            )
            name = tool_names.get(tool_call_id, "workspace_tool")
            safe_content, was_clipped = _clip_text_tool_result(agent, content)
            normalized.append(
                agent.ChatMessage(
                    role="user",
                    content=f"Tool result for {name}:\n{safe_content}",
                )
            )
            converted_tool_results += 1
            if was_clipped:
                clipped_tool_results += 1
            continue

        normalized.append(message)

    if not replaced_system:
        normalized.insert(0, agent.ChatMessage(role="system", content=fresh_system))

    return normalized, {
        "removed_empty_assistant_messages": removed_empty_assistant,
        "converted_tool_calls": converted_tool_calls,
        "converted_tool_results": converted_tool_results,
        "clipped_tool_results": clipped_tool_results,
    }


def materialize_request(
    agent: Any,
    req: Any,
    task: Mapping[str, Any],
    *,
    source_backend: str,
    backend: str,
    upstream_model: str,
) -> tuple[Any, coding_execution_policy.ExecutionPolicySnapshot, dict[str, Any]]:
    effective_task = coding_execution_policy.execution_task(agent, task)
    snapshot = coding_execution_policy.capture(
        agent,
        effective_task,
        backend=backend,
        upstream_model=upstream_model,
    )
    coding_request = _is_coding_execution_request(req)
    messages = list(getattr(req, "messages", None) or [])
    diagnostics: dict[str, Any] = {
        "source_backend": str(source_backend or ""),
        "backend": snapshot.backend,
        "upstream_model": snapshot.upstream_model,
        "text_tool_mode": snapshot.text_tool_mode,
        "policy_signature": snapshot.signature,
        "coding_request": coding_request,
    }

    updates: dict[str, Any] = {}
    if coding_request:
        fresh_system = agent._system_prompt(
            effective_task,
            text_tool_mode=snapshot.text_tool_mode,
        )
        messages, conversion = _normalize_messages(
            agent,
            messages,
            text_tool_mode=snapshot.text_tool_mode,
            fresh_system=fresh_system,
        )
        diagnostics.update(conversion)
        before_compaction = len(messages)
        if snapshot.text_tool_mode:
            compact = getattr(agent, "_compact_text_tool_messages", None)
            if callable(compact):
                messages = list(compact(messages))
        diagnostics["history_messages_before_compaction"] = before_compaction
        diagnostics["history_messages_after_compaction"] = len(messages)
        updates["messages"] = messages
        updates["tools"] = (
            None
            if snapshot.text_tool_mode
            else agent._tool_specs_for_task(effective_task)
        )
        updates["tool_choice"] = None if snapshot.text_tool_mode else "auto"
        updates["parallel_tool_calls"] = (
            None
            if snapshot.text_tool_mode
            else getattr(req, "parallel_tool_calls", None)
        )
        updates["max_tokens"] = agent._max_completion_tokens_for_route(
            req.model,
            backend,
            upstream_model,
        )
    else:
        route_cap = agent._max_completion_tokens_for_route(
            req.model,
            backend,
            upstream_model,
        )
        current_cap = getattr(req, "max_tokens", None)
        updates["max_tokens"] = (
            min(int(current_cap), route_cap)
            if isinstance(current_cap, int) and current_cap > 0
            else route_cap
        )

    x_nexus = dict(getattr(req, "x_nexus", None) or {})
    x_nexus["coding_execution_policy"] = snapshot.as_dict()
    updates["x_nexus"] = x_nexus

    return _copy_request(req, **updates), snapshot, diagnostics


async def _record_policy_transition(
    agent: Any,
    cw: Any,
    task_id: str,
    *,
    task: Mapping[str, Any],
    snapshot: coding_execution_policy.ExecutionPolicySnapshot,
    diagnostics: Mapping[str, Any],
    cycle: int,
) -> None:
    previous = (
        task.get("agent_execution_policy")
        if isinstance(task.get("agent_execution_policy"), Mapping)
        else {}
    )
    previous_signature = str(previous.get("signature") or "")
    backend_changed = str(diagnostics.get("source_backend") or "") != snapshot.backend
    changed = previous_signature != snapshot.signature
    if not changed and not backend_changed:
        return
    payload = snapshot.as_dict()
    await asyncio.to_thread(
        agent._mutate_task,
        task_id,
        {"agent_execution_policy": payload},
    )
    await asyncio.to_thread(
        agent._append_event,
        task_id,
        {
            "type": "execution_policy_transition",
            "cycle": cycle,
            "previous_signature": previous_signature,
            "policy_signature": snapshot.signature,
            "previous_action_kind": str(previous.get("action_kind") or ""),
            "action_kind": snapshot.action_kind,
            "source_backend": str(diagnostics.get("source_backend") or ""),
            "backend": snapshot.backend,
            "upstream_model": snapshot.upstream_model,
            "text_tool_mode": snapshot.text_tool_mode,
            "allowed_tools": list(snapshot.allowed_tools),
            "causal_evidence_targets": list(snapshot.causal_evidence_targets),
            "acceptance_evidence_targets": list(
                snapshot.acceptance_evidence_targets
            ),
            "hypothesis_causal_evidence_linked": (
                snapshot.hypothesis_causal_evidence_linked
            ),
            "removed_empty_assistant_messages": int(
                diagnostics.get("removed_empty_assistant_messages") or 0
            ),
            "converted_tool_calls": int(diagnostics.get("converted_tool_calls") or 0),
            "converted_tool_results": int(
                diagnostics.get("converted_tool_results") or 0
            ),
            "clipped_tool_results": int(
                diagnostics.get("clipped_tool_results") or 0
            ),
            "history_messages_before_compaction": int(
                diagnostics.get("history_messages_before_compaction") or 0
            ),
            "history_messages_after_compaction": int(
                diagnostics.get("history_messages_after_compaction") or 0
            ),
            "summary": (
                "Rematerialized the coding request for the current controller policy "
                "and destination backend capabilities."
            ),
        },
    )


def build_failover_call(cw: Any, guarded: Any):
    agent = guarded._agent

    async def call_backend_chat_with_policy(
        req: Any,
        backend: str,
        upstream_model: str,
        *,
        task_id: str,
        cycle: int,
        user_settings: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], str, str]:
        if agent.user_llm.is_user_model_id(req.model):
            return await guarded._ORIGINAL_CALL_BACKEND_CHAT_WITH_RETRY(
                req,
                backend,
                upstream_model,
                task_id=task_id,
                cycle=cycle,
                user_settings=user_settings,
            )

        max_retries = agent._backend_retry_count()
        admission = agent.get_admission_controller()
        excluded_backends: set[str] = set()

        for attempt in range(max_retries + 1):
            selected = await guarded._acquire_backend_excluding(
                req.model,
                backend,
                upstream_model,
                task_id=task_id,
                cycle=cycle,
                attempt=attempt,
                excluded_backends=excluded_backends,
            )
            selected_backend = str(selected.get("backend") or backend)
            selected_model = str(selected.get("upstream_model") or upstream_model)
            try:
                latest_task = await asyncio.to_thread(cw.load_task, task_id)
                adapted_req, snapshot, diagnostics = materialize_request(
                    agent,
                    req,
                    latest_task,
                    source_backend=backend,
                    backend=selected_backend,
                    upstream_model=selected_model,
                )
                await _record_policy_transition(
                    agent,
                    cw,
                    task_id,
                    task=latest_task,
                    snapshot=snapshot,
                    diagnostics=diagnostics,
                    cycle=cycle,
                )
                resp = await agent.call_backend_chat(
                    adapted_req,
                    selected_backend,
                    selected_model,
                )
                return resp, selected_backend, selected_model
            except HTTPException as exc:
                if attempt >= max_retries or not agent._is_retryable_backend_error(exc):
                    raise
                previous_exclusions = set(excluded_backends)
                excluded_backends = coding_backend_failover.retry_exclusions_after_error(
                    excluded_backends,
                    backend=selected_backend,
                    exc=exc,
                )
                full_read_failover = (
                    selected_backend in excluded_backends
                    and selected_backend not in previous_exclusions
                )
                delay = 0.0 if full_read_failover else agent._backend_retry_delay(attempt)
                detail = coding_backend_failover.backend_error_detail(exc)
                event_type = "backend_failover" if full_read_failover else "backend_retry"
                await asyncio.to_thread(
                    agent._append_event,
                    task_id,
                    {
                        "type": event_type,
                        "cycle": cycle,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "delay_sec": round(delay, 1),
                        "backend": selected_backend,
                        "upstream_model": selected_model,
                        "excluded_backends": sorted(excluded_backends),
                        "error": agent._clip_text(str(detail), 1200),
                        "summary": (
                            "A full generation read timeout exhausted this backend for "
                            "the current request; the next attempt must use another "
                            "healthy coding route."
                            if full_read_failover
                            else "Retrying a transient coding-backend failure."
                        ),
                    },
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            finally:
                admission.release(selected_backend, "chat")

        raise HTTPException(
            status_code=502,
            detail={"upstream": backend, "error": "retry loop exhausted"},
        )

    return call_backend_chat_with_policy


def install(cw: Any, guarded: Any) -> None:
    agent = guarded._agent
    if bool(getattr(agent, "_execution_dispatch_installed", False)):
        return
    replacement = build_failover_call(cw, guarded)
    guarded._call_backend_chat_with_failover = replacement
    agent._call_backend_chat_with_retry = replacement
    agent._execution_dispatch_installed = True
