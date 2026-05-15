from __future__ import annotations

import asyncio
import re
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.backends import backend_hostname, get_admission_controller, get_registry, llm_backends
from app import coding_workspace as cw
from app.config import S, logger
from app.health_checker import get_health_checker
from app.model_aliases import get_aliases
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec
from app.openai_utils import new_id, now_unix
from app.router import decide_route, default_model_for_backend
from app.router_cfg import router_cfg
from app.tools_bus import tool_web_browse
from app.upstreams import call_backend_chat


class _CodingAgentPaused(Exception):
    pass


_RUNNING: Dict[str, asyncio.Task[Any]] = {}


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


def _max_completion_tokens() -> int:
    try:
        return max(128, min(int(getattr(S, "CODING_AGENT_MAX_TOKENS", 512) or 512), 8192))
    except Exception:
        return 512


def _tool_context_char_limit() -> int:
    try:
        return max(2_000, min(int(getattr(S, "CODING_AGENT_TOOL_CONTEXT_CHARS", 10_000) or 10_000), 100_000))
    except Exception:
        return 10_000


def _backend_retry_count() -> int:
    try:
        return max(0, min(int(getattr(S, "CODING_AGENT_BACKEND_RETRIES", 2) or 0), 5))
    except Exception:
        return 2


def _backend_retry_delay(attempt_index: int) -> float:
    try:
        base = max(0.0, float(getattr(S, "CODING_AGENT_BACKEND_RETRY_BASE_DELAY_SEC", 10.0) or 0.0))
    except Exception:
        base = 10.0
    try:
        max_delay = max(base, float(getattr(S, "CODING_AGENT_BACKEND_RETRY_MAX_DELAY_SEC", 60.0) or 60.0))
    except Exception:
        max_delay = 60.0
    return min(max_delay, base * (2 ** max(0, attempt_index)))


def _backend_retry_statuses() -> set[int]:
    raw = str(getattr(S, "CODING_AGENT_BACKEND_RETRY_STATUSES", "500,502,503,504") or "")
    out: set[int] = set()
    for part in re.split(r"[\s,]+", raw):
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            continue
    return out or {500, 502, 503, 504}


def _coding_queue_timeout_sec() -> float:
    try:
        return max(1.0, float(getattr(S, "CODING_AGENT_QUEUE_TIMEOUT_SEC", 30.0) or 30.0))
    except Exception:
        return 30.0


def _coding_queue_poll_sec() -> float:
    try:
        return max(0.1, float(getattr(S, "CODING_AGENT_QUEUE_POLL_SEC", 1.0) or 1.0))
    except Exception:
        return 1.0


def _model_is_reroutable(model: str) -> bool:
    value = str(model or "").strip()
    if not value:
        return True
    key = value.lower()
    if key == "auto":
        return True
    if key in get_aliases():
        return True
    if ":" in value:
        return False
    registry = get_registry()
    return registry.get_backend(value) is None and "/" not in value


def _backend_supports_tool_calling(backend_name: str) -> bool:
    registry = get_registry()
    config = registry.get_backend(backend_name)
    if config is None:
        return False
    policy = config.payload_policy if isinstance(config.payload_policy, dict) else {}
    explicit = policy.get("supports_tool_calling")
    if explicit is not None:
        return bool(explicit)
    return str(config.provider or "").strip().lower() == "mlx"


def _candidate_summary(candidate: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "backend": candidate.get("backend"),
        "host": candidate.get("host"),
        "ready": bool(candidate.get("ready")),
        "available": int(candidate.get("available") or 0),
        "limit": int(candidate.get("limit") or 0),
        "inflight": int(candidate.get("inflight") or 0),
    }
    if candidate.get("health_error"):
        summary["health_error"] = str(candidate.get("health_error"))
    return summary


def _coding_candidate_routes(request_model: str, preferred_backend: str, preferred_upstream_model: str) -> List[tuple[str, str]]:
    out: List[tuple[str, str]] = []
    seen: set[str] = set()

    def add(backend_name: str, upstream_model_name: str) -> None:
        key = str(backend_name or "").strip()
        model_name = str(upstream_model_name or "").strip()
        if not key or not model_name or key in seen:
            return
        seen.add(key)
        out.append((key, model_name))

    if _backend_supports_tool_calling(preferred_backend):
        add(preferred_backend, preferred_upstream_model)
    if not _model_is_reroutable(request_model):
        return out

    cfg = router_cfg()
    for backend_name, _config in llm_backends():
        if not _backend_supports_tool_calling(backend_name):
            continue
        add(backend_name, default_model_for_backend(backend_name, cfg))
    return out


def _rank_coding_backend_candidates(request_model: str, preferred_backend: str, preferred_upstream_model: str) -> List[Dict[str, Any]]:
    registry = get_registry()
    checker = get_health_checker()
    stats = get_admission_controller().get_stats()

    host_totals: Dict[str, Dict[str, int]] = {}
    for key, stat in stats.items():
        if not str(key).endswith(".chat") or not isinstance(stat, dict):
            continue
        backend_name = str(key).rsplit(".", 1)[0]
        config = registry.get_backend(backend_name)
        if config is None:
            continue
        host = backend_hostname(backend_name, registry=registry, fallback_base_url=config.base_url) or backend_name
        bucket = host_totals.setdefault(host, {"limit": 0, "inflight": 0})
        bucket["limit"] += max(1, int(stat.get("limit") or config.get_limit("chat")))
        bucket["inflight"] += max(0, int(stat.get("inflight") or 0))

    candidates: List[Dict[str, Any]] = []
    for backend_name, upstream_model_name in _coding_candidate_routes(request_model, preferred_backend, preferred_upstream_model):
        config = registry.get_backend(backend_name)
        if config is None or not config.supports("chat"):
            continue
        stat = stats.get(f"{backend_name}.chat") if isinstance(stats, dict) else None
        limit = max(1, int((stat or {}).get("limit") or config.get_limit("chat")))
        available = max(0, int((stat or {}).get("available") or limit))
        inflight = max(0, int((stat or {}).get("inflight") or 0))
        host = backend_hostname(backend_name, registry=registry, fallback_base_url=config.base_url) or backend_name
        host_bucket = host_totals.get(host) or {"limit": limit, "inflight": inflight}
        host_limit = max(1, int(host_bucket.get("limit") or limit))
        host_inflight = max(0, int(host_bucket.get("inflight") or inflight))
        status = checker.get_status(backend_name)
        ready = checker.is_ready(backend_name)
        health_error = str(status.error or "").strip() if status is not None and status.error else ""
        candidates.append(
            {
                "backend": backend_name,
                "upstream_model": upstream_model_name,
                "host": host,
                "ready": ready,
                "health_error": health_error,
                "limit": limit,
                "available": available,
                "inflight": inflight,
                "host_limit": host_limit,
                "host_inflight": host_inflight,
                "host_load": host_inflight / host_limit,
                "backend_load": inflight / limit,
                "preferred": backend_name == preferred_backend,
            }
        )

    candidates.sort(
        key=lambda item: (
            0 if item["ready"] and item["available"] > 0 else 1 if item["ready"] else 2,
            item["host_load"],
            item["backend_load"],
            0 if not item["preferred"] else 1,
            str(item["backend"]),
        )
    )
    return candidates


async def _acquire_coding_backend_slot(
    request_model: str,
    preferred_backend: str,
    preferred_upstream_model: str,
    *,
    task_id: str,
    cycle: int,
    attempt: int,
) -> Dict[str, Any]:
    admission = get_admission_controller()
    deadline = time.monotonic() + _coding_queue_timeout_sec()
    queued_logged = False
    last_candidates: List[Dict[str, Any]] = []
    last_ready_count = 0

    while True:
        candidates = _rank_coding_backend_candidates(request_model, preferred_backend, preferred_upstream_model)
        last_candidates = candidates
        last_ready_count = sum(1 for item in candidates if item.get("ready"))
        for candidate in candidates:
            if not candidate.get("ready") or int(candidate.get("available") or 0) <= 0:
                continue
            try:
                await admission.acquire(str(candidate["backend"]), "chat")
            except HTTPException as exc:
                if int(getattr(exc, "status_code", 0) or 0) == 429:
                    continue
                raise
            if attempt > 0 or str(candidate.get("backend") or "") != preferred_backend:
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "backend_selected",
                        "cycle": cycle,
                        "attempt": attempt + 1,
                        "backend": candidate.get("backend"),
                        "upstream_model": candidate.get("upstream_model"),
                        "host": candidate.get("host"),
                        "preferred_backend": preferred_backend,
                        "preferred_upstream_model": preferred_upstream_model,
                        "summary": (
                            f"selected {candidate.get('backend')} on {candidate.get('host')} "
                            f"(preferred {preferred_backend})"
                        ),
                    },
                )
            return candidate

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            status_code = 429 if last_ready_count > 0 else 503
            raise HTTPException(
                status_code=status_code,
                detail={
                    "error": "coding_backend_queue_timeout" if status_code == 429 else "coding_backend_unavailable",
                    "message": "No healthy coding backend became available before the queue timeout elapsed",
                    "cycle": cycle,
                    "preferred_backend": preferred_backend,
                    "candidates": [_candidate_summary(item) for item in last_candidates[:6]],
                },
                headers={"Retry-After": str(max(1, int(_coding_queue_poll_sec())))} if status_code == 429 else None,
            )
        if not queued_logged:
            await asyncio.to_thread(
                _append_event,
                task_id,
                {
                    "type": "backend_wait",
                    "cycle": cycle,
                    "attempt": attempt + 1,
                    "timeout_sec": round(_coding_queue_timeout_sec(), 1),
                    "preferred_backend": preferred_backend,
                    "candidates": [_candidate_summary(item) for item in last_candidates[:6]],
                },
            )
            queued_logged = True
        await asyncio.sleep(min(_coding_queue_poll_sec(), max(0.05, remaining)))


def _backend_error_detail(exc: HTTPException) -> Dict[str, Any]:
    if isinstance(exc.detail, dict):
        return exc.detail
    return {"error": str(exc.detail)}


def _is_retryable_backend_error(exc: HTTPException) -> bool:
    detail = _backend_error_detail(exc)
    upstream_status = detail.get("status")
    try:
        upstream_status_i = int(upstream_status)
    except Exception:
        upstream_status_i = 0
    if upstream_status_i in _backend_retry_statuses():
        return True
    if int(getattr(exc, "status_code", 0) or 0) in {502, 503, 504}:
        body = str(detail.get("body") or detail.get("error") or "")
        transient_markers = ("internal_error", "timeout", "timed out", "temporarily unavailable", "connection")
        return not body or any(marker in body.lower() for marker in transient_markers)
    return False


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
        return f"checkpoint {status} cycle={event.get('cycle') or ''} commit={commit_hash[:12]}".strip()
    if event_type == "interrupted":
        return f"interrupted {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "no_tool_call":
        return f"no_tool_call cycle={event.get('cycle') or ''} {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "no_change_audit":
        return f"no_change_audit {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
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
    if event_type == "backend_retry":
        return (
            f"backend_retry cycle={event.get('cycle') or ''} attempt={event.get('attempt') or ''}/{event.get('max_retries') or ''} "
            f"delay={event.get('delay_sec') or ''}s {_clip_text(str(event.get('error') or ''), 500)}"
        ).strip()
    if event_type == "backend_wait":
        return (
            f"backend_wait cycle={event.get('cycle') or ''} attempt={event.get('attempt') or ''} "
            f"preferred={event.get('preferred_backend') or ''} timeout={event.get('timeout_sec') or ''}s"
        ).strip()
    if event_type == "backend_selected":
        return (
            f"backend_selected cycle={event.get('cycle') or ''} backend={event.get('backend') or ''} "
            f"host={event.get('host') or ''} preferred={event.get('preferred_backend') or ''}"
        ).strip()
    if event_type in {"queued", "started", "cycle_started", "review", "commit", "completed", "failed", "paused", "stopped"}:
        summary = str(event.get("summary") or event.get("error") or "")
        return f"{event_type} {_clip_text(summary, 700)}".strip()
    return f"{event_type} {_clip_text(json.dumps(event, ensure_ascii=False, sort_keys=True), 700)}"


def _no_change_audit(
    *,
    finish_called: bool,
    finish_success: bool,
    finish_summary: str,
    committed_changes: bool,
    uncommitted_changes: bool,
    start_head: str,
    end_head: str,
) -> tuple[bool, str, Optional[Dict[str, Any]]]:
    if committed_changes or uncommitted_changes:
        return finish_success, finish_summary, None
    if finish_called:
        audit_summary = (
            "The coding agent called coding_finish, but the workspace audit found no file changes and no commits "
            "created during the run. Marking the run failed instead of completed."
        )
        merged_summary = f"{audit_summary}\n\nAgent summary:\n{finish_summary}".strip() if finish_summary else audit_summary
        return (
            False,
            merged_summary,
            {
                "type": "no_change_audit",
                "ok": False,
                "summary": audit_summary,
                "start_commit": start_head,
                "end_commit": end_head,
            },
        )
    if not finish_success:
        audit_summary = (
            "The coding agent run ended without any file changes or commits in the workspace. "
            "Runs should only be trusted as completed work after they actually modify files."
        )
        merged_summary = f"{finish_summary}\n\nNo-change audit:\n{audit_summary}".strip() if finish_summary else audit_summary
        return (
            False,
            merged_summary,
            {
                "type": "no_change_audit",
                "ok": False,
                "summary": audit_summary,
                "start_commit": start_head,
                "end_commit": end_head,
            },
        )
    return finish_success, finish_summary, None


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


def _request_expects_workspace_edits(task: Dict[str, Any]) -> bool:
    original = str(task.get("prompt") or "").strip().lower()
    current = _effective_run_prompt(task).lower()
    text = f"{original}\n{current}"
    positive_markers = (
        "fix",
        "debug",
        "repair",
        "resolve",
        "implement",
        "edit",
        "modify",
        "update",
        "change",
        "patch",
        "root cause",
    )
    negative_markers = (
        "review this workspace",
        "review scope",
        "review only",
        "audit",
        "findings",
        "behavioral regressions",
        "missing tests",
    )
    if any(marker in text for marker in positive_markers):
        return True
    if any(marker in text for marker in negative_markers):
        return False
    return False


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
        if key in {"content", "patch", "old_text", "new_text"}:
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


def _has_incomplete_text_tool_call(content: Any) -> bool:
    text = content if isinstance(content, str) else ""
    if not text:
        return False
    lower = text.lower()
    if lower.count("<tool_call>") > lower.count("</tool_call>"):
        return True
    if lower.count("<function=") > lower.count("</function>"):
        return True
    if lower.count("<parameter=") > lower.count("</parameter>"):
        return True
    return False


def _tool_message_for_result(*, tool_call_id: str, result: Dict[str, Any]) -> ChatMessage:
    compact = _clip_jsonable(result, _tool_context_char_limit())
    return ChatMessage(role="tool", tool_call_id=tool_call_id, content=json.dumps(compact, separators=(",", ":"), ensure_ascii=False))


async def _call_backend_chat_with_retry(
    req: ChatCompletionRequest,
    backend: str,
    upstream_model: str,
    *,
    task_id: str,
    cycle: int,
) -> tuple[Dict[str, Any], str, str]:
    max_retries = _backend_retry_count()
    admission = get_admission_controller()
    for attempt in range(max_retries + 1):
        selected = await _acquire_coding_backend_slot(
            req.model,
            backend,
            upstream_model,
            task_id=task_id,
            cycle=cycle,
            attempt=attempt,
        )
        selected_backend = str(selected.get("backend") or backend)
        selected_model = str(selected.get("upstream_model") or upstream_model)
        try:
            resp = await call_backend_chat(req, selected_backend, selected_model)
            return resp, selected_backend, selected_model
        except HTTPException as exc:
            if attempt >= max_retries or not _is_retryable_backend_error(exc):
                raise
            delay = _backend_retry_delay(attempt)
            detail = _backend_error_detail(exc)
            await asyncio.to_thread(
                _append_event,
                task_id,
                {
                    "type": "backend_retry",
                    "cycle": cycle,
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "delay_sec": round(delay, 1),
                    "backend": selected_backend,
                    "upstream_model": selected_model,
                    "error": _clip_text(str(detail), 1200),
                },
            )
            logger.warning(
                "coding agent retrying backend task=%s cycle=%s backend=%s model=%s attempt=%s/%s delay=%.1fs error=%s",
                task_id,
                cycle,
                selected_backend,
                selected_model,
                attempt + 1,
                max_retries,
                delay,
                detail,
            )
            if delay > 0:
                await asyncio.sleep(delay)
        finally:
            admission.release(selected_backend, "chat")
    raise HTTPException(status_code=502, detail={"upstream": backend, "error": "retry loop exhausted"})


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
                name="coding_tool_manifest",
                description="List the coding tools currently available in this workspace with usage guidance.",
                parameters={
                    "type": "object",
                    "properties": {
                        "include_parameters": {"type": "boolean"},
                    },
                },
            )
        ),
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
                description="Read a complete UTF-8 text file from the coding workspace repository. Prefer coding_read_file_lines for large files.",
                parameters={
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string", "description": "Repository-relative file path."}},
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_read_file_lines",
                description="Read a bounded 1-based line range from a UTF-8 text file.",
                parameters={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string", "description": "Repository-relative file path."},
                        "start_line": {"type": "integer", "minimum": 1},
                        "line_count": {"type": "integer", "minimum": 1, "maximum": 2000},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_write_file",
                description="Write a complete UTF-8 text file inside the coding workspace repository. Prefer coding_replace_text or coding_apply_patch for focused edits.",
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
                name="coding_replace_text",
                description="Replace an exact text span in one UTF-8 file. Use for small, precise edits after reading the target file.",
                parameters={
                    "type": "object",
                    "required": ["path", "old_text", "new_text"],
                    "properties": {
                        "path": {"type": "string", "description": "Repository-relative file path."},
                        "old_text": {"type": "string", "description": "Exact text to replace."},
                        "new_text": {"type": "string", "description": "Replacement text."},
                        "expected_replacements": {"type": "integer", "description": "Expected match count. Default is 1. Use -1 to allow any positive count."},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_apply_patch",
                description="Apply a unified diff to repository files after validating patch paths stay inside the workspace.",
                parameters={
                    "type": "object",
                    "required": ["patch"],
                    "properties": {
                        "patch": {"type": "string", "description": "Unified diff text, with repository-relative paths."},
                        "check_only": {"type": "boolean", "description": "Validate the patch without applying it."},
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
                        "fixed_strings": {"type": "boolean", "description": "Treat query as a literal string."},
                        "case_sensitive": {"type": "boolean", "description": "Default true."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_fetch_url",
                description="Use the shared Gateway web browsing tool to fetch a public HTTP/HTTPS page as readable text with links and metadata.",
                parameters={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string"},
                        "max_bytes": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "extract_links": {"type": "boolean"},
                        "include_html": {"type": "boolean"},
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
                name="coding_change_summary",
                description="Return a compact summary of changed files by status.",
                parameters={"type": "object", "properties": {}},
            )
        ),
        ToolSpec(
            function=ToolFunction(
                name="coding_checkpoint",
                description="Create a local checkpoint commit for current workspace changes.",
                parameters={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Optional checkpoint commit message."},
                    },
                },
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


def coding_tool_manifest() -> Dict[str, Any]:
    tools = [spec.model_dump(exclude_none=True) for spec in _tool_specs()]
    guidance = [
        "Use coding_tool_manifest when you need to inspect your workspace tool capabilities.",
        "Use coding_list_tree, coding_search_text, and coding_read_file_lines before broad reads or edits.",
        "Prefer coding_replace_text for exact focused edits and coding_apply_patch for multi-file diffs.",
        "Use coding_fetch_url for current public documentation or issue pages.",
        "Inspect coding_git_diff or coding_change_summary before calling coding_finish.",
    ]
    return {
        "tools": tools,
        "guidance": guidance,
        "tool_names": [
            str(((item.get("function") or {}).get("name") if isinstance(item.get("function"), dict) else ""))
            for item in tools
            if isinstance(item, dict)
        ],
    }


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


def _pause_requested(task_id: str) -> bool:
    try:
        task = cw.load_task(task_id)
        return bool(task.get("agent_stop_requested") or task.get("agent_pause_requested"))
    except Exception:
        return False


def _raise_if_paused(task_id: str) -> None:
    if _pause_requested(task_id):
        raise _CodingAgentPaused("Coding run was paused. Start another run on this workspace to resume from the latest files and checkpoint.")


def _new_guidance_since(task_id: str, seen_count: int) -> tuple[List[Dict[str, Any]], int]:
    task = cw.load_task(task_id)
    messages = _guidance_messages(task)
    if seen_count < 0:
        seen_count = 0
    if seen_count >= len(messages):
        return [], len(messages)
    return messages[seen_count:], len(messages)


def _run_tool(task_id: str, name: str, args: Dict[str, Any], *, git_token_value: Optional[str]) -> Dict[str, Any]:
    if name == "coding_list_tree":
        return cw.list_tree(task_id, path=str(args.get("path") or ""), limit=int(args.get("limit") or 250))
    if name == "coding_tool_manifest":
        manifest = coding_tool_manifest()
        if not bool(args.get("include_parameters", False)):
            slim_tools = []
            for item in manifest.get("tools") or []:
                if not isinstance(item, dict):
                    continue
                fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                slim_tools.append(
                    {
                        "name": str(fn.get("name") or ""),
                        "description": str(fn.get("description") or ""),
                    }
                )
            manifest["tools"] = slim_tools
        return {"ok": True, **manifest}
    if name == "coding_read_file":
        return cw.read_file(task_id, path=str(args.get("path") or ""))
    if name == "coding_read_file_lines":
        return cw.read_file_lines(
            task_id,
            path=str(args.get("path") or ""),
            start_line=int(args.get("start_line") or 1),
            line_count=int(args.get("line_count") or 200),
        )
    if name == "coding_write_file":
        return cw.write_file(task_id, path=str(args.get("path") or ""), content=str(args.get("content") or ""))
    if name == "coding_replace_text":
        expected = args.get("expected_replacements", 1)
        if expected is not None:
            expected = int(expected)
            if expected < 0:
                expected = None
        return cw.replace_text(
            task_id,
            path=str(args.get("path") or ""),
            old_text=str(args.get("old_text") or ""),
            new_text=str(args.get("new_text") or ""),
            expected_replacements=expected,
        )
    if name == "coding_apply_patch":
        return cw.apply_unified_patch(task_id, patch=str(args.get("patch") or ""), check_only=bool(args.get("check_only")))
    if name == "coding_search_text":
        return cw.search_text(
            task_id,
            query=str(args.get("query") or ""),
            path=str(args.get("path") or ""),
            glob=str(args.get("glob") or ""),
            fixed_strings=bool(args.get("fixed_strings")),
            case_sensitive=bool(args.get("case_sensitive", True)),
            limit=int(args.get("limit") or 200),
        )
    if name == "coding_fetch_url":
        return tool_web_browse(
            {
                "url": str(args.get("url") or ""),
                "max_bytes": args.get("max_bytes"),
                "timeout_sec": args.get("timeout_sec"),
                "extract_links": bool(args.get("extract_links", True)),
                "include_html": bool(args.get("include_html")),
            }
        )
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
    if name == "coding_change_summary":
        return cw.git_change_summary(task_id)
    if name == "coding_checkpoint":
        message = str(args.get("message") or "").strip() or f"Nexus coding agent checkpoint for {task_id}"
        return cw.checkpoint_task(task_id, message=message)
    if name == "coding_finish":
        return {"ok": True, "summary": str(args.get("summary") or ""), "success": bool(args.get("success", True))}
    raise HTTPException(status_code=400, detail=f"unknown coding tool: {name}")


def _checkpoint_enabled() -> bool:
    return bool(getattr(S, "CODING_AGENT_CHECKPOINT_COMMITS", True))


def _checkpoint_after_cycle(task_id: str, *, run_id: str, cycle: int) -> Dict[str, Any]:
    msg = f"Nexus checkpoint: {task_id} cycle {cycle}"
    return cw.checkpoint_task(task_id, message=msg, run_id=run_id, cycle=cycle)


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
    edit_expectation = ""
    if _request_expects_workspace_edits(task):
        edit_expectation = (
            "This request is fix-oriented. After you identify the concrete root cause, make the smallest viable workspace edit "
            "that addresses it, run a targeted validation step, inspect the resulting diff, and only then finish. "
            "Do not stop at diagnosis alone when a focused fix is available. "
        )
    return (
        "You are Nexus Coding Agent. Work autonomously toward the user's coding request inside one isolated git workspace. "
        "Use the provided tools to inspect, edit, and test the repository. Do not ask the user for routine next steps. "
        "Treat workspace conversation messages as additional user guidance. If new guidance arrives during a run, adjust during the next work cycle. "
        "Prefer this loop: inspect relevant files, make focused edits, run targeted checks, inspect git diff, then finish. "
        "Keep assistant responses concise; call tools promptly instead of narrating long plans. "
        "Do not push, open pull requests, force-push, rewrite git history, or modify files outside the workspace. "
        "The Gateway may create local checkpoint commits during the run so paused or interrupted work can resume from durable git history. "
        "Call coding_tool_manifest if you need to inspect the exact tools and guidance currently available in this workspace. "
        "Prefer coding_read_file_lines for targeted inspection. Avoid reading full large files unless needed; use coding_search_text first, then focused line ranges. "
        "Prefer coding_replace_text for exact small edits and coding_apply_patch for multi-file diffs; use coding_write_file only for whole-file rewrites or new files. "
        "Use coding_fetch_url for public documentation or issue pages when current external information is needed. "
        "Use coding_search_text before reading many files. "
        "Never finish by writing a prose-only assistant message; the run only ends when you call coding_finish. "
        "Call coding_finish only after you have either completed the task or identified a concrete blocker. "
        "If the request requires code or documentation changes, make edits and inspect the diff before calling coding_finish. "
        f"{edit_expectation}"
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
            "coding_model": model,
            "agent_run_id": run_id,
            "agent_status": "queued",
            "agent_model": model,
            "agent_backend": "",
            "agent_upstream_model": "",
            "agent_cycle": 0,
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
            "agent_pause_requested": False,
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


async def request_pause(task_id: str) -> Dict[str, Any]:
    await asyncio.to_thread(
        _mutate_task,
        task_id,
        {
            "agent_stop_requested": True,
            "agent_pause_requested": True,
            "agent_status": "pausing",
            "agent_last_event_at": now_unix(),
        },
    )
    await asyncio.to_thread(_append_event, task_id, {"type": "pause_requested"})
    running = _RUNNING.get(task_id)
    if running is not None and not running.done():
        running.cancel()
    fresh = await asyncio.to_thread(cw.load_task, task_id)
    return cw.public_task(fresh)


async def request_stop(task_id: str) -> Dict[str, Any]:
    return await request_pause(task_id)


async def _run_agent(
    task_id: str,
    *,
    run_id: str,
    git_token_value: Optional[str],
    model: str,
    auto_commit: bool,
    commit_message: Optional[str],
) -> None:
    t0 = time.monotonic()
    finish_summary = ""
    finish_success = False
    finish_called = False
    backend = ""
    upstream_model = ""
    try:
        task = await asyncio.to_thread(cw.load_task, task_id)
        start_head_result = await asyncio.to_thread(cw.git_head, task_id)
        start_head = str(start_head_result.get("commit") or "")
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
        no_tool_cycles = 0
        cycle = 0

        while True:
            cycle += 1
            _raise_if_paused(task_id)
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
                            "cycle": cycle,
                            "count": len(new_guidance),
                            "summary": _clip_text(guidance_text, 1000),
                        },
                    )
            await asyncio.to_thread(_mutate_task, task_id, {"agent_cycle": cycle, "agent_last_event_at": now_unix()})
            await asyncio.to_thread(_append_event, task_id, {"type": "cycle_started", "cycle": cycle})

            req = ChatCompletionRequest(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=_max_completion_tokens(),
                stream=False,
            )
            resp, backend, upstream_model = await _call_backend_chat_with_retry(
                req,
                backend,
                upstream_model,
                task_id=task_id,
                cycle=cycle,
            )
            await asyncio.to_thread(
                _mutate_task,
                task_id,
                {
                    "agent_backend": backend,
                    "agent_upstream_model": upstream_model,
                    "agent_last_event_at": now_unix(),
                },
            )
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
                        "cycle": cycle,
                        "thinking": _clip_text(thinking, 4000),
                    },
                )
            await asyncio.to_thread(
                _append_event,
                task_id,
                {
                    "type": "assistant",
                    "cycle": cycle,
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
                no_tool_cycles += 1
                malformed_text_tool_call = _has_incomplete_text_tool_call(assistant.content)
                notice = (
                    "The assistant started a text-form tool call, but it was malformed or truncated before it could be executed. "
                    "The next response must emit one complete workspace tool call."
                    if malformed_text_tool_call
                    else "The assistant responded without a tool call. A coding run cannot complete from prose alone; "
                    "it must call workspace tools and then coding_finish."
                )
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "no_tool_call",
                        "cycle": cycle,
                        "count": no_tool_cycles,
                        "summary": notice,
                        "malformed_text_tool_call": malformed_text_tool_call,
                        "content": _clip_text(str(assistant.content or ""), 2000),
                    },
                )
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            "Your previous response did not produce an executable workspace tool call. Continue the coding task now by calling one of the "
                            "provided tools, such as coding_list_tree, coding_read_file_lines, coding_replace_text, coding_apply_patch, "
                            "coding_git_diff, or coding_finish. Do not answer with a prose-only plan. "
                            "If you emit a text-form tool call, respond with exactly one complete <tool_call>{...}</tool_call> block and nothing else."
                            if malformed_text_tool_call or no_tool_cycles >= 2
                            else "Your previous response did not call any workspace tool. Continue the coding task now by calling one of the "
                            "provided tools, such as coding_read_file_lines, coding_replace_text, coding_apply_patch, "
                            "coding_git_diff, or coding_finish. Do not answer with a prose-only plan."
                        ),
                    )
                )
                continue
            no_tool_cycles = 0

            stop_after_tools = False
            for tc in tool_calls:
                _raise_if_paused(task_id)
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(fn.get("name") or "").strip()
                args = _parse_tool_arguments(fn.get("arguments", ""))
                tool_call_id = tc.get("id") if isinstance(tc.get("id"), str) else new_id("toolcall")
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {"type": "tool_started", "cycle": cycle, "tool_call_id": tool_call_id, "name": name, "args": _safe_args_preview(name, args)},
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
                        "cycle": cycle,
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "result": _event_result(result),
                    },
                )
                messages.append(_tool_message_for_result(tool_call_id=tool_call_id, result=result))

                if name == "coding_finish":
                    finish_summary = str(result.get("summary") or args.get("summary") or "").strip()
                    finish_success = bool(result.get("success", args.get("success", True)))
                    finish_called = True
                    stop_after_tools = True
                    break

            if _checkpoint_enabled():
                checkpoint = await asyncio.to_thread(_checkpoint_after_cycle, task_id, run_id=run_id, cycle=cycle)
                if checkpoint.get("changed") or not checkpoint.get("ok"):
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "checkpoint",
                            "cycle": cycle,
                            "ok": bool(checkpoint.get("ok")),
                            "changed": bool(checkpoint.get("changed")),
                            "commit": str(checkpoint.get("last_commit") or ""),
                            "error": str(checkpoint.get("error") or ""),
                            "result": _event_result(checkpoint),
                        },
                    )

            if stop_after_tools:
                break

        status_result = await asyncio.to_thread(cw.git_status, task_id, git_token_value=git_token_value)
        diff_result = await asyncio.to_thread(cw.git_diff, task_id)
        change_summary = await asyncio.to_thread(cw.git_change_summary, task_id)
        end_head_result = await asyncio.to_thread(cw.git_head, task_id)
        end_head = str(end_head_result.get("commit") or "")
        change_counts = change_summary.get("counts") if isinstance(change_summary.get("counts"), dict) else {}
        uncommitted_changes = int(change_counts.get("total") or 0) > 0
        committed_changes = bool(start_head and end_head and start_head != end_head)
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "review",
                "status": _event_result(status_result),
                "diff": _event_result(diff_result),
                "change_summary": _event_result(change_summary),
                "start_commit": start_head,
                "end_commit": end_head,
                "committed_changes": committed_changes,
                "uncommitted_changes": uncommitted_changes,
            },
        )

        finish_success, finish_summary, audit_event = _no_change_audit(
            finish_called=finish_called,
            finish_success=finish_success,
            finish_summary=finish_summary,
            committed_changes=committed_changes,
            uncommitted_changes=uncommitted_changes,
            start_head=start_head,
            end_head=end_head,
        )
        if audit_event is not None:
            await asyncio.to_thread(_append_event, task_id, audit_event)

        if auto_commit and finish_success:
            msg = str(commit_message or finish_summary or "").strip()
            if not msg:
                msg = "Apply Nexus coding agent changes"
            msg = msg.splitlines()[0][:160]
            commit_result = await asyncio.to_thread(cw.commit_task, task_id, message=msg)
            if not commit_result.get("ok") and str(commit_result.get("error") or "") == "no changes to commit":
                latest_task = await asyncio.to_thread(cw.load_task, task_id)
                existing_commit = str(latest_task.get("last_commit") or latest_task.get("last_checkpoint_commit") or "")
                summary = "No uncommitted changes remained after checkpoint commits."
                if existing_commit:
                    summary = f"Changes were already saved in checkpoint commit {existing_commit[:12]}."
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "commit",
                        "message": msg,
                        "skipped": True,
                        "summary": summary,
                        "commit": existing_commit,
                        "result": _event_result(commit_result),
                    },
                )
            else:
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "commit",
                        "message": msg,
                        "ok": bool(commit_result.get("ok")),
                        "commit": str(commit_result.get("last_commit") or ""),
                        "result": _event_result(commit_result),
                    },
                )
                if not commit_result.get("ok"):
                    finish_success = False
                    finish_summary = (
                        f"Agent completed the coding work, but auto-commit failed: "
                        f"{commit_result.get('error') or 'git commit failed'}"
                    )

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
                "agent_status": "paused",
                "agent_error": "",
                "agent_summary": "Coding run was paused. Start another run on this workspace to resume from the latest files and checkpoint.",
                "agent_finished_at": time.time(),
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {"type": "paused", "summary": "Coding run was paused. Start another run on this workspace to resume from the latest files and checkpoint."},
        )
        raise
    except _CodingAgentPaused as exc:
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "paused",
                "agent_error": "",
                "agent_summary": str(exc),
                "agent_finished_at": time.time(),
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(_append_event, task_id, {"type": "paused", "summary": str(exc)})
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
