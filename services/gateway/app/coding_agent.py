from __future__ import annotations

import asyncio
import re
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app import coding_workspace as cw
from app.config import S, logger
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec
from app.openai_utils import new_id, now_unix
from app.router import decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_backend_chat


class _CodingAgentStopped(Exception):
    pass


_RUNNING: Dict[str, asyncio.Task[Any]] = {}


def _max_turns(requested: Optional[int] = None) -> int:
    default = int(getattr(S, "CODING_AGENT_MAX_TURNS", 12) or 12)
    try:
        value = int(requested) if requested is not None else default
    except Exception:
        value = default
    return max(1, min(value, 50))


def _max_runtime_sec(requested: Optional[float] = None) -> float:
    default = float(getattr(S, "CODING_AGENT_MAX_RUNTIME_SEC", 1800) or 1800)
    try:
        value = float(requested) if requested is not None else default
    except Exception:
        value = default
    return max(30.0, min(value, max(30.0, default), 7200.0))


def _max_events() -> int:
    try:
        return max(20, min(int(getattr(S, "CODING_AGENT_MAX_EVENTS", 120) or 120), 1000))
    except Exception:
        return 120


def _tool_result_char_limit() -> int:
    try:
        return max(2_000, min(int(getattr(S, "CODING_AGENT_MAX_TOOL_RESULT_CHARS", 60_000) or 60_000), 500_000))
    except Exception:
        return 60_000


def _clip_text(value: Any, limit: int = 12_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    keep = max(200, limit // 2)
    return f"{text[:keep]}\n\n[... truncated {len(text) - limit} chars ...]\n\n{text[-keep:]}"


def _clip_jsonable(value: Any, limit: Optional[int] = None) -> Any:
    char_limit = _tool_result_char_limit() if limit is None else max(500, int(limit))
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return _clip_text(str(value), char_limit)
    if len(raw) <= char_limit:
        return value
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        budget_each = max(500, char_limit // max(1, len(value)))
        for key, item in value.items():
            if isinstance(item, str):
                out[key] = _clip_text(item, budget_each)
            elif isinstance(item, (dict, list)):
                out[key] = _clip_jsonable(item, budget_each)
            else:
                out[key] = item
        try:
            if len(json.dumps(out, ensure_ascii=False, sort_keys=True)) <= char_limit:
                return out
        except Exception:
            pass
    return {"truncated": True, "text": _clip_text(raw, char_limit)}


def _event_result(value: Any) -> Any:
    return _clip_jsonable(value, min(_tool_result_char_limit(), 20_000))


def _event_digest_line(event: Dict[str, Any]) -> str:
    event_type = str(event.get("type") or "event")
    if event_type == "thinking":
        return f"thinking {_clip_text(str(event.get('thinking') or event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "checkpoint":
        commit_hash = str(event.get("commit") or "").strip()
        status = "saved" if event.get("ok") else "failed"
        return f"checkpoint {status} turn={event.get('turn') or ''} commit={commit_hash[:12]}".strip()
    if event_type == "interrupted":
        return f"interrupted {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "assistant":
        calls = event.get("tool_calls")
        names = []
        if isinstance(calls, list):
            for item in calls:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item.get("name")))
        content = _clip_text(str(event.get("content") or "").strip(), 700)
        return f"assistant tools={names or []} {content}".strip()
    if event_type in {"tool_started", "tool_finished"}:
        name = str(event.get("name") or "")
        result = event.get("result")
        detail = ""
        if isinstance(result, dict):
            if result.get("error"):
                detail = f" error={_clip_text(result.get('error'), 500)}"
            elif result.get("summary"):
                detail = f" summary={_clip_text(result.get('summary'), 500)}"
            elif result.get("path"):
                detail = f" path={result.get('path')}"
        return f"{event_type} {name}{detail}".strip()
    if event_type in {"queued", "started", "turn_started", "review", "commit", "completed", "failed", "stopped"}:
        summary = str(event.get("summary") or event.get("error") or "")
        return f"{event_type} {_clip_text(summary, 700)}".strip()
    return f"{event_type} {_clip_text(json.dumps(event, ensure_ascii=False, sort_keys=True), 700)}"


def _previous_run_context(task: Dict[str, Any]) -> str:
    previous_status = str(task.get("agent_previous_status") or "").strip()
    previous_run_id = str(task.get("agent_previous_run_id") or "").strip()
    previous_summary = str(task.get("agent_previous_summary") or "").strip()
    previous_error = str(task.get("agent_previous_error") or "").strip()
    events = task.get("agent_events")
    event_lines: List[str] = []
    if isinstance(events, list):
        for item in events[-16:]:
            if isinstance(item, dict):
                event_lines.append(f"- {_event_digest_line(item)}")
    if not previous_status and not event_lines:
        return ""
    bits = [
        "Continuation context:",
        f"- Previous run id: {previous_run_id or '(unknown)'}",
        f"- Previous status: {previous_status or '(unknown)'}",
    ]
    if previous_summary:
        bits.append(f"- Previous summary: {_clip_text(previous_summary, 1200)}")
    if previous_error:
        bits.append(f"- Previous error: {_clip_text(previous_error, 1200)}")
    if event_lines:
        bits.append("- Recent prior events:")
        bits.extend(event_lines)
    bits.append("Continue from the current workspace files and git diff. Do not repeat completed work unless needed.")
    return "\n".join(bits)


def _guidance_messages(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages = task.get("guidance_messages")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


def _effective_run_prompt(task: Dict[str, Any]) -> str:
    prompt = str(task.get("agent_run_prompt") or "").strip()
    if prompt:
        return prompt
    return str(task.get("prompt") or "").strip()


def _guidance_context(task: Dict[str, Any], *, limit: int = 12) -> str:
    messages = _guidance_messages(task)[-max(1, limit):]
    if not messages:
        return ""
    lines = ["Workspace conversation and guidance:"]
    for item in messages:
        actor = str(item.get("actor") or "user").strip() or "user"
        ts = item.get("ts")
        when = ""
        try:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else ""
        except Exception:
            when = ""
        label = f"{actor} at {when}" if when else actor
        lines.append(f"- {label}: {_clip_text(str(item.get('content') or '').strip(), 1600)}")
    return "\n".join(lines)


def _safe_args_preview(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key == "content":
            out[key] = f"<{len(str(value or '').encode('utf-8'))} bytes>"
        elif isinstance(value, str):
            out[key] = _clip_text(value, 1000)
        else:
            out[key] = value
    if name == "coding_write_file" and "content" not in out:
        out["content"] = "<empty>"
    return out


def _extract_assistant_message(resp: Dict[str, Any]) -> ChatMessage:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    if not isinstance(msg, dict):
        msg = {}
    role = msg.get("role") if isinstance(msg.get("role"), str) else "assistant"
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    return ChatMessage(role=role, content=content, tool_calls=tool_calls)


def _extract_assistant_thinking(resp: Dict[str, Any]) -> str:
    choice = (resp.get("choices") or [{}])[0]
    msg = (choice.get("message") or {}) if isinstance(choice, dict) else {}
    if not isinstance(msg, dict):
        msg = {}
    parts: List[str] = []
    for source in (msg, choice if isinstance(choice, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("thinking", "reasoning", "reasoning_content", "reasoning_text"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, (dict, list)):
                try:
                    parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
                except Exception:
                    parts.append(str(value))
    return "\n\n".join(part for part in parts if part).strip()


def _extract_tool_calls(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    calls = (msg or {}).get("tool_calls")
    if isinstance(calls, list):
        return [item for item in calls if isinstance(item, dict)]
    return []


_TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.IGNORECASE | re.DOTALL)
_TEXT_FUNCTION_RE = re.compile(r"<function=([A-Za-z_][A-Za-z0-9_]*)>(.*?)</function>", re.IGNORECASE | re.DOTALL)
_TEXT_PARAMETER_RE = re.compile(r"<parameter=([A-Za-z_][A-Za-z0-9_]*)>(.*?)</parameter>", re.IGNORECASE | re.DOTALL)


def _coerce_text_tool_value(value: str) -> Any:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    if raw.lower() == "null":
        return None
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except Exception:
            return raw
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", raw):
        try:
            return float(raw)
        except Exception:
            return raw
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _text_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": new_id("toolcall"),
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, separators=(",", ":"), ensure_ascii=False)},
    }


def _extract_text_tool_calls(content: Any) -> List[Dict[str, Any]]:
    text = content if isinstance(content, str) else ""
    if "<tool_call>" not in text.lower():
        return []
    out: List[Dict[str, Any]] = []
    for block in _TEXT_TOOL_CALL_RE.findall(text):
        block = str(block or "").strip()
        if not block:
            continue

        if block.startswith("{"):
            try:
                parsed = json.loads(block)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                name = str(parsed.get("name") or parsed.get("tool") or parsed.get("function") or "").strip()
                args = parsed.get("arguments") or parsed.get("args") or {}
                if isinstance(args, str):
                    args = _parse_tool_arguments(args)
                if name and isinstance(args, dict):
                    out.append(_text_tool_call(name, args))
                    continue

        fn = _TEXT_FUNCTION_RE.search(block)
        if not fn:
            continue
        name = str(fn.group(1) or "").strip()
        body = str(fn.group(2) or "")
        args: Dict[str, Any] = {}
        for param in _TEXT_PARAMETER_RE.finditer(body):
            key = str(param.group(1) or "").strip()
            if not key:
                continue
            args[key] = _coerce_text_tool_value(str(param.group(2) or ""))
        if name:
            out.append(_text_tool_call(name, args))
    return out


def _tool_message_for_result(*, tool_call_id: str, result: Dict[str, Any]) -> ChatMessage:
    compact = _clip_jsonable(result)
    return ChatMessage(role="tool", tool_call_id=tool_call_id, content=json.dumps(compact, separators=(",", ":"), ensure_ascii=False))


def _parse_tool_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _tool_specs() -> List[ToolSpec]:
    return [
        ToolSpec(
            function=ToolFunction(
                name="coding_list_tree",
                description="List files and directories inside the coding workspace repository.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repository-relative directory path. Empty string means repository root."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_read_file",
                description="Read a UTF-8 text file from the coding workspace repository.",
                parameters={
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string", "description": "Repository-relative file path."}},
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_write_file",
                description="Write a complete UTF-8 text file inside the coding workspace repository.",
                parameters={
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string", "description": "Repository-relative file path."},
                        "content": {"type": "string", "description": "Complete replacement file content."},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_search_text",
                description="Search repository text using ripgrep. Prefer this before opening many files.",
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "description": "Optional repository-relative path to limit the search."},
                        "glob": {"type": "string", "description": "Optional ripgrep glob, such as *.py or services/gateway/**."},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_run_command",
                description="Run an allowlisted argv command in the workspace. Use for targeted tests and non-destructive inspection.",
                parameters={
                    "type": "object",
                    "required": ["argv"],
                    "properties": {
                        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "cwd": {"type": "string", "description": "Optional repository-relative working directory."},
                        "timeout_sec": {"type": "number", "minimum": 1},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_git_status",
                description="Return git status for the workspace branch.",
                parameters={"type": "object", "properties": {}},
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_git_diff",
                description="Return the current staged and unstaged git diff for review.",
                parameters={"type": "object", "properties": {}},
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_finish",
                description="Finish the autonomous run when the requested coding work is complete or blocked.",
                parameters={
                    "type": "object",
                    "required": ["summary"],
                    "properties": {
                        "summary": {"type": "string"},
                        "success": {"type": "boolean", "description": "True if the task is complete enough for user review."},
                    },
                },
            )
        ),
    ]


def _mutate_task(task_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    task.update(fields)
    cw.save_task(task)
    return task


def _append_event(task_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    events = task.get("agent_events")
    if not isinstance(events, list):
        events = []
    ev = {"ts": now_unix(), **event}
    events.append(ev)
    task["agent_events"] = events[-_max_events():]
    task["agent_last_event_at"] = ev["ts"]
    cw.save_task(task)
    return ev


def _stop_requested(task_id: str) -> bool:
    try:
        task = cw.load_task(task_id)
        return bool(task.get("agent_stop_requested"))
    except Exception:
        return False


def _raise_if_stopped(task_id: str) -> None:
    if _stop_requested(task_id):
        raise _CodingAgentStopped("coding run stop requested")


def _new_guidance_since(task_id: str, seen_count: int) -> tuple[List[Dict[str, Any]], int]:
    task = cw.load_task(task_id)
    messages = _guidance_messages(task)
    if seen_count < 0:
        seen_count = 0
    if seen_count >= len(messages):
        return [], len(messages)
    return messages[seen_count:], len(messages)


def _search_text(task_id: str, args: Dict[str, Any], *, git_token_value: Optional[str]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    if len(query) > 500:
        raise HTTPException(status_code=400, detail="query is too long")
    argv = ["rg", "-n", "--hidden", "--glob", "!.git"]
    glob = str(args.get("glob") or "").strip()
    if glob:
        argv.extend(["--glob", glob])
    argv.extend(["--", query])
    path = str(args.get("path") or "").strip().lstrip("/\\")
    if path:
        argv.append(path)
    return cw.run_task_command(task_id, argv=argv, git_token_value=git_token_value)


def _run_tool(task_id: str, name: str, args: Dict[str, Any], *, git_token_value: Optional[str]) -> Dict[str, Any]:
    if name == "coding_list_tree":
        return cw.list_tree(task_id, path=str(args.get("path") or ""), limit=int(args.get("limit") or 250))
    if name == "coding_read_file":
        return cw.read_file(task_id, path=str(args.get("path") or ""))
    if name == "coding_write_file":
        return cw.write_file(task_id, path=str(args.get("path") or ""), content=str(args.get("content") or ""))
    if name == "coding_search_text":
        return _search_text(task_id, args, git_token_value=git_token_value)
    if name == "coding_run_command":
        argv = args.get("argv")
        if not isinstance(argv, list):
            raise HTTPException(status_code=400, detail="argv must be a list")
        return cw.run_task_command(
            task_id,
            argv=[str(item) for item in argv],
            cwd=str(args.get("cwd") or ""),
            timeout_sec=args.get("timeout_sec"),
            git_token_value=git_token_value,
        )
    if name == "coding_git_status":
        return cw.git_status(task_id, git_token_value=git_token_value)
    if name == "coding_git_diff":
        return cw.git_diff(task_id)
    if name == "coding_finish":
        return {"ok": True, "summary": str(args.get("summary") or ""), "success": bool(args.get("success", True))}
    raise HTTPException(status_code=400, detail=f"unknown coding tool: {name}")


def _checkpoint_enabled() -> bool:
    return bool(getattr(S, "CODING_AGENT_CHECKPOINT_COMMITS", True))


def _checkpoint_after_turn(task_id: str, *, run_id: str, turn: int) -> Dict[str, Any]:
    msg = f"Nexus checkpoint: {task_id} turn {turn}"
    return cw.checkpoint_task(task_id, message=msg, run_id=run_id, turn=turn)


def _system_prompt(task: Dict[str, Any]) -> str:
    allowed = ", ".join(cw.allowed_commands())
    original = str(task.get("prompt") or "").strip()
    current = _effective_run_prompt(task)
    guidance = _guidance_context(task)
    request_bits = [f"Original user request:\n{original or '(none recorded)'}"]
    if current and current != original:
        request_bits.append(f"Current run request:\n{current}")
    if guidance:
        request_bits.append(guidance)
    return (
        "You are Nexus Coding Agent. Work autonomously toward the user's coding request inside one isolated git workspace. "
        "Use the provided tools to inspect, edit, and test the repository. Do not ask the user for routine next steps. "
        "Treat workspace conversation messages as additional user guidance. If new guidance arrives during a run, adjust the plan on the next turn. "
        "Prefer this loop: inspect relevant files, make focused edits, run targeted checks, inspect git diff, then finish. "
        "Do not push, open pull requests, force-push, rewrite git history, or modify files outside the workspace. "
        "The Gateway may create local checkpoint commits between turns so interrupted runs can resume from durable git history. "
        "Write complete replacement file contents when using coding_write_file. "
        "Use coding_search_text before reading many files. "
        "Call coding_finish only after you have either completed the task or identified a concrete blocker. "
        f"Allowed commands are: {allowed or '(none)'}. "
        f"Workspace task id: {task.get('id')}. Base branch: {task.get('base_branch')}. Working branch: {task.get('branch_name')}.\n\n"
        + "\n\n".join(request_bits)
    )


def _task_context(task: Dict[str, Any]) -> str:
    original = str(task.get("prompt") or "").strip()
    current = _effective_run_prompt(task)
    base = (
        f"Original user request:\n{original}\n\n"
        f"Current run request:\n{current or original}\n\n"
        f"Repository: {cw.redact_repo_url(str(task.get('repo_url') or ''))}\n"
        f"Branch: {task.get('branch_name')} from {task.get('base_branch')}\n"
        "Start by inspecting the repository, then proceed without waiting for more user input."
    )
    guidance = _guidance_context(task)
    if guidance:
        base = f"{base}\n\n{guidance}"
    previous = _previous_run_context(task)
    if previous:
        return f"{base}\n\n{previous}"
    return base


def _choose_model(task: Dict[str, Any], requested_model: Optional[str]) -> str:
    return str(requested_model or task.get("coding_model") or "coder").strip() or "coder"


async def start_agent_run(
    task_id: str,
    *,
    git_token_value: Optional[str] = None,
    coding_model: Optional[str] = None,
    prompt: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_runtime_sec: Optional[float] = None,
    auto_commit: bool = False,
    commit_message: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    cw._ensure_enabled()
    task = await asyncio.to_thread(cw.load_task, task_id)
    status = str(task.get("status") or "")
    if status == "error":
        raise HTTPException(status_code=409, detail="workspace is in error state")
    running = _RUNNING.get(task_id)
    if running is not None and not running.done():
        raise HTTPException(status_code=409, detail="coding agent is already running for this workspace")

    turns = _max_turns(max_turns)
    runtime = _max_runtime_sec(max_runtime_sec)
    run_id = new_id("coderun")
    model = _choose_model(task, coding_model)
    previous_events = task.get("agent_events")
    if not isinstance(previous_events, list):
        previous_events = []
    previous_run_id = str(task.get("agent_run_id") or "")
    previous_status = str(task.get("agent_status") or "idle")
    previous_summary = str(task.get("agent_summary") or "")
    previous_error = str(task.get("agent_error") or "")
    requested_prompt = str(prompt or "").strip()
    effective_prompt = requested_prompt or str(task.get("prompt") or "").strip()
    guidance_messages = _guidance_messages(task)
    if requested_prompt:
        guidance_messages.append(
            {
                "ts": time.time(),
                "role": "user",
                "actor": str(actor or "").strip(),
                "run_id": run_id,
                "content": requested_prompt,
            }
        )
        guidance_messages = guidance_messages[-200:]
    now = time.time()
    await asyncio.to_thread(
        _mutate_task,
        task_id,
        {
            "agent_run_id": run_id,
            "agent_status": "queued",
            "agent_model": model,
            "agent_backend": "",
            "agent_upstream_model": "",
            "agent_turn": 0,
            "agent_max_turns": turns,
            "agent_started_at": now,
            "agent_finished_at": None,
            "agent_last_event_at": now_unix(),
            "agent_summary": "",
            "agent_error": "",
            "agent_previous_run_id": previous_run_id,
            "agent_previous_status": previous_status,
            "agent_previous_summary": previous_summary,
            "agent_previous_error": previous_error,
            "agent_stop_requested": False,
            "agent_auto_commit": bool(auto_commit),
            "agent_run_prompt": effective_prompt,
            "agent_events": previous_events[-_max_events():],
            "guidance_messages": guidance_messages,
            "last_guidance_at": guidance_messages[-1].get("ts") if requested_prompt and guidance_messages else task.get("last_guidance_at"),
        },
    )
    await asyncio.to_thread(
        _append_event,
        task_id,
        {
            "type": "queued",
            "run_id": run_id,
            "model": model,
            "max_turns": turns,
            "max_runtime_sec": runtime,
            "auto_commit": bool(auto_commit),
            "actor": actor or "",
            "continuation": bool(previous_run_id or previous_events),
            "previous_status": previous_status,
            "prompt": _clip_text(effective_prompt, 1000),
            "checkpoint_commits": _checkpoint_enabled(),
        },
    )
    job = asyncio.create_task(
        _run_agent(
            task_id,
            run_id=run_id,
            git_token_value=git_token_value,
            model=model,
            max_turns=turns,
            max_runtime_sec=runtime,
            auto_commit=bool(auto_commit),
            commit_message=commit_message,
        )
    )
    _RUNNING[task_id] = job

    def _cleanup(done: asyncio.Task[Any]) -> None:
        current = _RUNNING.get(task_id)
        if current is done:
            _RUNNING.pop(task_id, None)

    job.add_done_callback(_cleanup)
    fresh = await asyncio.to_thread(cw.load_task, task_id)
    return cw.public_task(fresh)


async def request_stop(task_id: str) -> Dict[str, Any]:
    await asyncio.to_thread(
        _mutate_task,
        task_id,
        {
            "agent_stop_requested": True,
            "agent_status": "stopping",
            "agent_last_event_at": now_unix(),
        },
    )
    await asyncio.to_thread(_append_event, task_id, {"type": "stop_requested"})
    running = _RUNNING.get(task_id)
    if running is not None and not running.done():
        running.cancel()
    fresh = await asyncio.to_thread(cw.load_task, task_id)
    return cw.public_task(fresh)


async def _run_agent(
    task_id: str,
    *,
    run_id: str,
    git_token_value: Optional[str],
    model: str,
    max_turns: int,
    max_runtime_sec: float,
    auto_commit: bool,
    commit_message: Optional[str],
) -> None:
    t0 = time.monotonic()
    finish_summary = ""
    finish_success = False
    backend = ""
    upstream_model = ""
    try:
        task = await asyncio.to_thread(cw.load_task, task_id)
        route = decide_route(
            cfg=router_cfg(),
            request_model=model,
            headers={"x-request-type": "coding"},
            messages=[{"role": "user", "content": _effective_run_prompt(task)}],
            has_tools=True,
            enable_policy=getattr(S, "ROUTER_ENABLE_POLICY", True),
            enable_request_type=True,
        )
        backend = route.backend
        upstream_model = route.model
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "running",
                "agent_backend": backend,
                "agent_upstream_model": upstream_model,
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "started",
                "run_id": run_id,
                "backend": backend,
                "upstream_model": upstream_model,
                "route_reason": route.reason,
            },
        )

        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=_system_prompt(task)),
            ChatMessage(role="user", content=_task_context(task)),
        ]
        seen_guidance_count = len(_guidance_messages(task))
        tools = _tool_specs()

        for turn in range(max_turns):
            _raise_if_stopped(task_id)
            if time.monotonic() - t0 > max_runtime_sec:
                raise HTTPException(status_code=408, detail="coding agent runtime budget exceeded")
            new_guidance, seen_guidance_count = await asyncio.to_thread(_new_guidance_since, task_id, seen_guidance_count)
            if new_guidance:
                guidance_text = "\n\n".join(
                    _clip_text(str(item.get("content") or "").strip(), 4000)
                    for item in new_guidance
                    if str(item.get("content") or "").strip()
                )
                if guidance_text:
                    messages.append(ChatMessage(role="user", content=f"User guidance update during this run:\n{guidance_text}"))
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "guidance_seen",
                            "turn": turn + 1,
                            "count": len(new_guidance),
                            "summary": _clip_text(guidance_text, 1000),
                        },
                    )
            await asyncio.to_thread(_mutate_task, task_id, {"agent_turn": turn + 1, "agent_last_event_at": now_unix()})
            await asyncio.to_thread(_append_event, task_id, {"type": "turn_started", "turn": turn + 1})

            req = ChatCompletionRequest(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                stream=False,
            )
            resp = await call_backend_chat(req, backend, upstream_model)
            assistant = _extract_assistant_message(resp)
            thinking = _extract_assistant_thinking(resp)
            tool_calls = _extract_tool_calls(resp)
            text_tool_calls = False
            if not tool_calls:
                tool_calls = _extract_text_tool_calls(assistant.content)
                text_tool_calls = bool(tool_calls)
            event_content = assistant.content if isinstance(assistant.content, str) else ""
            if text_tool_calls:
                assistant = ChatMessage(role=assistant.role, content=None, tool_calls=tool_calls)
            messages.append(assistant)
            if thinking:
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "thinking",
                        "turn": turn + 1,
                        "thinking": _clip_text(thinking, 4000),
                    },
                )
            await asyncio.to_thread(
                _append_event,
                task_id,
                {
                    "type": "assistant",
                    "turn": turn + 1,
                    "content": _clip_text(event_content, 4000),
                    "tool_call_format": "text" if text_tool_calls else "native",
                    "tool_calls": [
                        {
                            "id": item.get("id") if isinstance(item.get("id"), str) else "",
                            "name": ((item.get("function") or {}).get("name") if isinstance(item.get("function"), dict) else ""),
                        }
                        for item in tool_calls
                    ],
                },
            )

            if not tool_calls:
                finish_summary = str(assistant.content or "").strip()
                finish_success = True
                break

            stop_after_tools = False
            for tc in tool_calls:
                _raise_if_stopped(task_id)
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(fn.get("name") or "").strip()
                args = _parse_tool_arguments(fn.get("arguments", ""))
                tool_call_id = tc.get("id") if isinstance(tc.get("id"), str) else new_id("toolcall")
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {"type": "tool_started", "turn": turn + 1, "tool_call_id": tool_call_id, "name": name, "args": _safe_args_preview(name, args)},
                )
                try:
                    result = await asyncio.to_thread(_run_tool, task_id, name, args, git_token_value=git_token_value)
                except HTTPException as exc:
                    result = {"ok": False, "error": exc.detail}
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "tool_finished",
                        "turn": turn + 1,
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "result": _event_result(result),
                    },
                )
                messages.append(_tool_message_for_result(tool_call_id=tool_call_id, result=result))

                if name == "coding_finish":
                    finish_summary = str(result.get("summary") or args.get("summary") or "").strip()
                    finish_success = bool(result.get("success", args.get("success", True)))
                    stop_after_tools = True
                    break

            if _checkpoint_enabled():
                checkpoint = await asyncio.to_thread(_checkpoint_after_turn, task_id, run_id=run_id, turn=turn + 1)
                if checkpoint.get("changed") or not checkpoint.get("ok"):
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "checkpoint",
                            "turn": turn + 1,
                            "ok": bool(checkpoint.get("ok")),
                            "changed": bool(checkpoint.get("changed")),
                            "commit": str(checkpoint.get("last_commit") or ""),
                            "error": str(checkpoint.get("error") or ""),
                            "result": _event_result(checkpoint),
                        },
                    )

            if stop_after_tools:
                break

        if not finish_summary and not finish_success:
            finish_summary = (
                "Turn limit reached before the agent called coding_finish. "
                "Start another run on this same workspace to continue from the current files and diff."
            )
            finish_success = False

        status_result = await asyncio.to_thread(cw.git_status, task_id, git_token_value=git_token_value)
        diff_result = await asyncio.to_thread(cw.git_diff, task_id)
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "review",
                "status": _event_result(status_result),
                "diff": _event_result(diff_result),
            },
        )

        if auto_commit and finish_success:
            msg = str(commit_message or finish_summary or "").strip()
            if not msg:
                msg = "Apply Nexus coding agent changes"
            msg = msg.splitlines()[0][:160]
            commit_result = await asyncio.to_thread(cw.commit_task, task_id, message=msg)
            if not commit_result.get("ok") and str(commit_result.get("error") or "") == "no changes to commit":
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {"type": "commit", "message": msg, "skipped": True, "summary": "No uncommitted changes remained after checkpoint commits.", "result": _event_result(commit_result)},
                )
            else:
                await asyncio.to_thread(_append_event, task_id, {"type": "commit", "message": msg, "result": _event_result(commit_result)})

        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "completed" if finish_success else "failed",
                "agent_summary": finish_summary,
                "agent_error": "" if finish_success else finish_summary,
                "agent_finished_at": time.time(),
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "completed" if finish_success else "failed",
                "ok": finish_success,
                "summary": finish_summary,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
    except asyncio.CancelledError:
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "stopped",
                "agent_error": "",
                "agent_summary": "Coding run was stopped.",
                "agent_finished_at": time.time(),
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(_append_event, task_id, {"type": "stopped", "summary": "Coding run was stopped."})
        raise
    except _CodingAgentStopped as exc:
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "stopped",
                "agent_error": "",
                "agent_summary": str(exc),
                "agent_finished_at": time.time(),
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(_append_event, task_id, {"type": "stopped", "summary": str(exc)})
    except Exception as exc:
        error = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        logger.warning("coding agent failed task=%s backend=%s model=%s error=%s", task_id, backend, upstream_model, error)
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "failed",
                "agent_error": str(error),
                "agent_summary": "",
                "agent_finished_at": time.time(),
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "failed",
                "error": _clip_text(str(error), 4000),
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
