from __future__ import annotations

import asyncio
import re
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException

from app.backends import backend_hostname, get_admission_controller, get_registry, llm_backends
from app import coding_model_policy
from app import coding_workspace as cw
from app import user_llm, user_store
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
_ACTIVE_AGENT_STATUSES = {"queued", "running", "stopping", "pausing"}


def _max_events() -> int:
    try:
        return max(20, min(int(getattr(S, "CODING_AGENT_MAX_EVENTS", 120) or 120), 1000))
    except Exception:
        return 120


def _active_runner(task_id: str) -> Optional[asyncio.Task[Any]]:
    running = _RUNNING.get(task_id)
    if running is not None and not running.done():
        return running
    if running is not None and running.done():
        _RUNNING.pop(task_id, None)
    return None


def _mark_stale_agent_paused(task_id: str, task: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task = task if isinstance(task, dict) else cw.load_task(task_id)
    previous_status = str(task.get("agent_status") or "").strip().lower()
    if previous_status not in _ACTIVE_AGENT_STATUSES:
        return task
    summary = (
        "No active coding runner is attached to this workspace. "
        "The persisted run state was marked paused so manual commands and a later resume can proceed."
    )
    task.update(
        {
            "agent_status": "paused",
            "agent_stop_requested": False,
            "agent_pause_requested": False,
            "agent_summary": summary,
            "agent_error": "",
            "agent_finished_at": time.time(),
            "agent_last_event_at": now_unix(),
        }
    )
    cw.save_task(task)
    _append_event(
        task_id,
        {
            "type": "stale_agent_recovered",
            "previous_status": previous_status,
            "summary": summary,
        },
    )
    return cw.load_task(task_id)


async def recover_stale_agent_run(task_id: str) -> Dict[str, Any]:
    task = await asyncio.to_thread(cw.load_task, task_id)
    status = str(task.get("agent_status") or "").strip().lower()
    if status in _ACTIVE_AGENT_STATUSES and _active_runner(task_id) is None:
        task = await asyncio.to_thread(_mark_stale_agent_paused, task_id, task)
    return cw.public_task(task)


async def recover_stale_agent_runs() -> Dict[str, Any]:
    if not cw.coding_enabled():
        return {"ok": True, "recovered": 0, "tasks": []}
    recovered: List[str] = []

    def _recover_all() -> List[str]:
        cw._ensure_dirs()
        changed: List[str] = []
        for path in cw.tasks_dir().glob("code_*.json"):
            try:
                task = cw._read_json(path)
            except Exception:
                continue
            task_id = str(task.get("id") or path.stem)
            status = str(task.get("agent_status") or "").strip().lower()
            if status in _ACTIVE_AGENT_STATUSES and _active_runner(task_id) is None:
                _mark_stale_agent_paused(task_id, task)
                changed.append(task_id)
        return changed

    recovered = await asyncio.to_thread(_recover_all)
    return {"ok": True, "recovered": len(recovered), "tasks": recovered}


def _tool_result_char_limit() -> int:
    try:
        return max(2_000, min(int(getattr(S, "CODING_AGENT_MAX_TOOL_RESULT_CHARS", 100_000) or 100_000), 500_000))
    except Exception:
        return 100_000


def _max_completion_tokens() -> int:
    try:
        return max(128, min(int(getattr(S, "CODING_AGENT_MAX_TOKENS", 8192) or 8192), 8192))
    except Exception:
        return 8192


def _text_tool_max_completion_tokens() -> int:
    try:
        return max(64, min(int(getattr(S, "CODING_AGENT_TEXT_TOOL_MAX_TOKENS", 256) or 256), 2048))
    except Exception:
        return 256


def _max_completion_tokens_for_route(model: str, backend: str) -> int:
    cap = _max_completion_tokens()
    alias = get_aliases().get(str(model or "").strip().lower())
    if alias is not None and alias.max_tokens_cap is not None:
        cap = min(cap, int(alias.max_tokens_cap))
    if not _backend_supports_tool_calling(backend):
        cap = min(cap, _text_tool_max_completion_tokens())
    return cap


def _tool_context_char_limit() -> int:
    try:
        return max(2_000, min(int(getattr(S, "CODING_AGENT_TOOL_CONTEXT_CHARS", 32_000) or 32_000), 100_000))
    except Exception:
        return 32_000


def _max_no_tool_call_cycles() -> int:
    try:
        return max(2, min(int(getattr(S, "CODING_AGENT_MAX_NO_TOOL_CYCLES", 4) or 4), 20))
    except Exception:
        return 4


def _max_semantic_reroutes() -> int:
    try:
        return max(0, min(int(getattr(S, "CODING_AGENT_MAX_SEMANTIC_REROUTES", 1) or 1), 5))
    except Exception:
        return 1


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


def _preferred_route_supports_coding_tools(request_model: str, preferred_backend: str) -> bool:
    if _backend_supports_tool_calling(preferred_backend):
        return True
    alias = get_aliases().get(str(request_model or "").strip().lower())
    if alias is None or alias.tools is not True:
        return False
    registry = get_registry()
    alias_backend = registry.resolve_backend_class(alias.backend) or alias.backend
    resolved_preferred = registry.resolve_backend_class(preferred_backend) or preferred_backend
    return alias_backend == resolved_preferred


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

    if _preferred_route_supports_coding_tools(request_model, preferred_backend):
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
            0 if item["preferred"] else 1,
            item["host_load"],
            item["backend_load"],
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


def _compact_event(task: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(event)
    event_type = str(out.get("type") or "")
    events = task.get("agent_events")
    last_event = events[-1] if isinstance(events, list) and events else None

    if event_type == "assistant":
        calls = out.get("tool_calls") if isinstance(out.get("tool_calls"), list) else []
        content = _clip_text(str(out.get("content") or "").strip(), 2000 if calls else 800)
        if not calls:
            if isinstance(last_event, dict) and str(last_event.get("type") or "") == "assistant":
                last_content = str(last_event.get("content") or "").strip()
                if content and content == last_content:
                    content = "(same unverified model output as previous cycle)"
            out["summary"] = str(out.get("summary") or "Unverified model output before any workspace tool executed.")[:400]
        out["content"] = content
    elif event_type == "thinking":
        out["thinking"] = _clip_text(str(out.get("thinking") or out.get("summary") or "").strip(), 1200)
    elif event_type == "no_tool_call":
        out["content"] = _clip_text(str(out.get("content") or "").strip(), 600)
        out["summary"] = _clip_text(str(out.get("summary") or "").strip(), 700)

    return out


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
    if event_type == "no_tool_call_limit":
        return f"no_tool_call_limit cycle={event.get('cycle') or ''} {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "no_change_audit":
        return f"no_change_audit {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "finish_gate":
        return f"finish_gate {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "semantic_reroute":
        return (
            f"semantic_reroute cycle={event.get('cycle') or ''} "
            f"{event.get('previous_backend') or ''}->{event.get('backend') or ''}"
        ).strip()
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
    if event_type == "idle_deferred":
        return f"idle_deferred {_clip_text(str(event.get('summary') or ''), 700)}".strip()
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
    expects_workspace_edits: bool = True,
) -> tuple[bool, str, Optional[Dict[str, Any]]]:
    if committed_changes or uncommitted_changes:
        return finish_success, finish_summary, None
    if not expects_workspace_edits and finish_called:
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


def _tool_result_modified_workspace(name: str, args: Dict[str, Any], result: Dict[str, Any]) -> bool:
    if not bool(result.get("ok")):
        return False
    if name == "coding_write_file":
        return True
    if name == "coding_replace_text":
        try:
            return int(result.get("replacements") or 0) > 0
        except Exception:
            return True
    if name == "coding_apply_patch":
        if bool(args.get("check_only")) or bool(result.get("check_only")):
            return False
        apply_result = result.get("apply")
        if isinstance(apply_result, dict):
            return bool(apply_result.get("ok"))
        return True
    return False


def _is_python_validation_command(parts: List[str]) -> bool:
    if not parts:
        return False
    lowered = [item.lower() for item in parts]
    if "-m" in lowered:
        index = lowered.index("-m")
        module = lowered[index + 1] if index + 1 < len(lowered) else ""
        root_module = module.split(".", 1)[0]
        return root_module in {"pytest", "unittest", "py_compile", "compileall", "ruff", "mypy"}
    script = Path(parts[0]).name.lower()
    if script in {"pytest", "ruff", "mypy"}:
        return True
    return script.startswith("test_") and script.endswith(".py")


def _is_validation_command(argv: Any) -> bool:
    if not isinstance(argv, list) or not argv:
        return False
    parts = [str(item).strip() for item in argv if str(item).strip()]
    if not parts:
        return False
    cmd = Path(parts[0]).name.lower()
    lowered = [item.lower() for item in parts]
    if cmd in {"pytest", "ruff", "mypy"}:
        return True
    if cmd in {"python", "python3"}:
        return _is_python_validation_command(parts[1:])
    if cmd == "node":
        return any(item in {"--check", "--test"} for item in lowered[1:])
    if cmd == "npm":
        if len(lowered) >= 2 and lowered[1] in {"test", "t"}:
            return True
        if len(lowered) >= 3 and lowered[1] == "run":
            script = lowered[2]
            return any(marker in script for marker in ("test", "lint", "typecheck", "check", "build"))
        return False
    if cmd == "uv":
        meaningful = {item for item in lowered[1:] if not item.startswith("-")}
        if meaningful.intersection({"pytest", "ruff", "mypy", "py_compile", "compileall", "unittest"}):
            return True
        if "--check" in lowered or "--test" in lowered:
            return True
        return any(marker in item for item in meaningful for marker in ("test", "lint", "typecheck"))
    if cmd == "git":
        return len(lowered) >= 3 and lowered[1] == "diff" and "--check" in lowered[2:]
    return False


def _validation_command_failed_due_to_missing_tool(result: Dict[str, Any]) -> bool:
    if bool(result.get("ok")):
        return False
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    missing_markers = (
        "command not found:",
        "no module named pytest",
        "no module named ruff",
        "no module named mypy",
        "modulenotfounderror: no module named",
        "executable file not found",
        "not recognized as an internal or external command",
    )
    return any(marker in text for marker in missing_markers)


def _request_text(task: Dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in [
            str(task.get("prompt") or "").strip(),
            _effective_run_prompt(task),
        ]
        if part
    ).lower()


def _diff_added_lines(diff_text: str) -> List[str]:
    lines: List[str] = []
    for raw in str(diff_text or "").splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        lines.append(raw[1:])
    return lines


def _finish_gate_placeholder_feedback(diff_result: Optional[Dict[str, Any]]) -> str:
    diff_text = str((((diff_result or {}).get("diff") or {}).get("stdout") or ""))
    if not diff_text:
        return ""
    placeholder_markers = (
        "add logic to",
        "no tests yet",
        "placeholder",
        "todo",
        "stub",
    )
    for line in _diff_added_lines(diff_text):
        lowered = line.strip().lower()
        if any(marker in lowered for marker in placeholder_markers):
            return (
                "The latest diff still contains placeholder or stub text instead of a concrete implementation. "
                "Replace placeholder handlers or fake tests with real logic before reporting success."
            )
    return ""


def _finish_gate_manifest_feedback(
    task: Dict[str, Any],
    *,
    diff_result: Optional[Dict[str, Any]],
    validation_argv_after_edit: Optional[Sequence[str]],
) -> str:
    changes = (diff_result or {}).get("changes") if isinstance(diff_result, dict) else {}
    files = changes.get("files") if isinstance(changes, dict) else []
    if not isinstance(files, list):
        return ""
    added_files = {
        str(item.get("path") or "")
        for item in files
        if isinstance(item, dict) and str(item.get("status") or "").upper() == "A"
    }
    if "services/gateway/package.json" not in added_files:
        return ""
    request_text = _request_text(task)
    node_markers = ("package.json", "npm", "node", "javascript", "typescript", "frontend", "webapp")
    if any(marker in request_text for marker in node_markers):
        return ""
    argv = [str(item).strip().lower() for item in (validation_argv_after_edit or []) if str(item).strip()]
    if argv and Path(argv[0]).name.lower() == "npm":
        return (
            "The latest edits introduced services/gateway/package.json only to support npm-based validation, but this request did not ask for Node project scaffolding. "
            "Remove the invented package manifest and validate the real change with an existing checker such as node --check, pytest, or git diff --check."
        )
    return ""


def _finish_gate_feedback(
    *,
    task: Dict[str, Any],
    finish_success: bool,
    workspace_modified: bool,
    diff_reviewed_after_edit: bool,
    validation_run_after_edit: bool,
    validation_ok_after_edit: Optional[bool],
    validation_failed_after_edit: bool = False,
    diff_result_after_edit: Optional[Dict[str, Any]] = None,
    validation_argv_after_edit: Optional[Sequence[str]] = None,
) -> str:
    if not finish_success or not workspace_modified:
        return ""
    placeholder_feedback = _finish_gate_placeholder_feedback(diff_result_after_edit)
    if placeholder_feedback:
        return placeholder_feedback
    manifest_feedback = _finish_gate_manifest_feedback(
        task,
        diff_result=diff_result_after_edit,
        validation_argv_after_edit=validation_argv_after_edit,
    )
    if manifest_feedback:
        return manifest_feedback
    if validation_failed_after_edit:
        return (
            "A validation command failed after the latest edit. Fix the reported issue and rerun validation, "
            "or call coding_finish with success=false and a concrete blocker."
        )
    missing: List[str] = []
    if not validation_run_after_edit:
        missing.append(
            "run a targeted validation command after the latest edit, such as pytest, ruff check, "
            "python -m py_compile, node --check, npm test, or git diff --check. If one checker is unavailable, use an available fallback"
        )
    elif validation_ok_after_edit is False:
        return (
            "You ran validation after editing, but it failed. Fix the reported issue and rerun validation, "
            "or call coding_finish with success=false and a concrete blocker."
        )
    if not diff_reviewed_after_edit:
        missing.append("inspect the actual workspace diff with coding_git_diff after the latest edit")
    if not missing:
        return ""
    return "Before reporting success after workspace edits, " + " and ".join(missing) + "."


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

    def has_positive(value: str) -> bool:
        return any(marker in value for marker in positive_markers)

    def has_negative(value: str) -> bool:
        return any(marker in value for marker in negative_markers)

    def looks_answer_only(value: str) -> bool:
        stripped = value.strip()
        if not stripped:
            return False
        question_starts = (
            "what ",
            "why ",
            "how ",
            "when ",
            "where ",
            "who ",
            "can you explain",
            "explain ",
            "summarize",
            "status",
            "tell me",
            "show me",
            "did you",
        )
        if "?" in stripped:
            return True
        return stripped.startswith(question_starts) or " status" in stripped or "summary" in stripped

    def has_direct_edit_intent(value: str) -> bool:
        direct_markers = (
            "fix ",
            "debug ",
            "repair ",
            "resolve ",
            "implement ",
            "edit ",
            "modify ",
            "patch ",
            "update the ",
            "change the ",
            "add ",
            "remove ",
        )
        return any(marker in value for marker in direct_markers)

    if current and current != original:
        if (looks_answer_only(current) or has_negative(current)) and not has_direct_edit_intent(current):
            return False
        if has_positive(current):
            return True
        return has_positive(original) and not has_negative(original)

    text = f"{original}\n{current}"
    if has_positive(text):
        return True
    if has_negative(text) or looks_answer_only(current or original):
        return False
    return False


def _model_integration_context(task: Dict[str, Any]) -> str:
    integration = task.get("integration")
    if not isinstance(integration, dict):
        return ""
    strategy = str(integration.get("integration_strategy") or "").strip()
    if strategy == "existing_vllm_model":
        deployment = integration.get("deployment_target") if isinstance(integration.get("deployment_target"), dict) else {}
        return (
            "Model integration mode: existing vLLM model lane.\n"
            f"- HuggingFace model: {integration.get('model_id') or ''}\n"
            f"- Existing backend lane: {integration.get('backend_class') or deployment.get('backend_lane') or ''}\n"
            f"- Target host: {deployment.get('host') or ''}\n"
            "- Treat this as a model availability/configuration change on the existing vLLM lane. Do not create a new backend class, service directory, Dockerfile, registrar, or lifecycle backend unless repo evidence proves the existing lane cannot serve the model.\n"
            "- Preserve the repository root README and broad documentation; make focused patches or use generated integration notes."
        )
    return (
        "Model integration mode: generated backend scaffold.\n"
        f"- HuggingFace model: {integration.get('model_id') or ''}\n"
        f"- Runtime: {integration.get('runtime') or ''}\n"
        f"- Route kind: {integration.get('route_kind') or ''}\n"
        "- Preserve existing repository documentation and fill in the generated scaffold with focused edits."
    )


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


def _text_tool_result_message(*, name: str, result: Dict[str, Any]) -> ChatMessage:
    compact = _clip_jsonable(result, min(_tool_context_char_limit(), 2000))
    payload = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
    return ChatMessage(
        role="user",
        content=(
            f"Tool result for {name}:\n{payload}\n\n"
            "Continue the coding task with exactly one complete <tool_call>{...}</tool_call> block, "
            "or call coding_finish when the task is complete or blocked."
        ),
    )


async def _call_backend_chat_with_retry(
    req: ChatCompletionRequest,
    backend: str,
    upstream_model: str,
    *,
    task_id: str,
    cycle: int,
    user_settings: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], str, str]:
    if user_llm.is_user_model_id(req.model):
        parsed = user_llm.parse_user_model_id(req.model)
        provider, selected_model = parsed if parsed is not None else ("user", upstream_model)
        selected_backend = user_llm.user_backend_name(provider)
        resp = await user_llm.call_user_chat(req, model_id=req.model, settings=user_settings or {})
        return resp, selected_backend, selected_model

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
        return _coerce_tool_arguments(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"_raw": raw}
    return _coerce_tool_arguments(parsed) if isinstance(parsed, dict) else {"value": parsed}


def _coerce_tool_arguments(args: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(args)
    argv = out.get("argv")
    if isinstance(argv, str) and argv.strip().startswith("["):
        try:
            parsed_argv = json.loads(argv)
        except Exception:
            parsed_argv = None
        if isinstance(parsed_argv, list):
            out["argv"] = parsed_argv
    return out


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
                description="Write a complete UTF-8 text file inside the coding workspace repository. Use for new files or intentional whole-file rewrites only; prefer coding_replace_text or coding_apply_patch for existing files and focused edits.",
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
                description=(
                    "Finish the autonomous run when the requested coding work is complete or blocked. "
                    "A successful finish after edits is rejected unless a validation command and coding_git_diff ran after the latest edit."
                ),
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
        "Commands run inside a Linux workspace shell. Use POSIX paths, forward slashes, and Linux command/env syntax such as ls, cat, grep, python3, VAR=value cmd, and $VAR.",
        "Do not assume PowerShell, cmd.exe, drive letters, backslashes, %VAR%, or $env:VAR inside the workspace.",
        "Use coding_list_tree, coding_search_text, and coding_read_file_lines before broad reads or edits.",
        "Prefer coding_replace_text for exact focused edits and coding_apply_patch for multi-file diffs.",
        "Use coding_fetch_url for current public documentation or issue pages.",
        "Do not invent imports, functions, methods, variables, or config keys; search and read definitions before using them.",
        "Keep imports consolidated and avoid loading the same library multiple times.",
        "If a service owns its own package root, run validation from that service directory, for example cwd=services/gateway for gateway tests that import app.",
        "After editing, run a targeted validation command such as pytest, ruff check, python -m py_compile, node --check, npm test, or git diff --check.",
        "Do not invent package.json files, lockfiles, requirements files, or placeholder tests just to make validation pass. Only add project-manifest or dependency files when the user explicitly asked for that scaffolding or the target service already uses it.",
        "Placeholder handlers or comments like 'Add logic to ...' do not count as a fix.",
        "After editing, inspect coding_git_diff before calling coding_finish.",
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


def _text_tool_call_guidance() -> str:
    tools = []
    for spec in _tool_specs():
        try:
            tools.append(str(spec.function.name))
        except Exception:
            continue
    names = ", ".join(tools)
    return (
        "This selected backend does not receive native OpenAI tool definitions. "
        "Use text-form tool calls instead. To call a tool, respond with exactly one complete block and no prose: "
        '<tool_call>{"name":"coding_read_file_lines","arguments":{"path":"README.md","start_line":1,"line_count":80}}</tool_call>. '
        "Use JSON only inside the block. Do not wrap the block in Markdown fences. "
        "Call coding_tool_manifest with include_parameters=true if you need exact parameter schemas. "
        f"Available tool names: {names}."
    )


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
    ev = _compact_event(task, {"ts": now_unix(), **event})
    events.append(ev)
    task["agent_events"] = events[-_max_events():]
    task["agent_last_event_at"] = ev["ts"]
    cw.save_task(task)
    return ev


def _semantic_reroute_candidate(
    request_model: str,
    backend: str,
    upstream_model: str,
    *,
    excluded_backends: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    blocked = {str(item).strip() for item in (excluded_backends or set()) if str(item).strip()}
    for candidate in _rank_coding_backend_candidates(request_model, backend, upstream_model):
        candidate_backend = str(candidate.get("backend") or "").strip()
        if not candidate_backend or candidate_backend == backend or candidate_backend in blocked:
            continue
        if not candidate.get("ready") or int(candidate.get("available") or 0) <= 0:
            continue
        return candidate
    return None


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


def _system_prompt(task: Dict[str, Any], *, text_tool_mode: bool = False) -> str:
    allowed = ", ".join(cw.allowed_commands())
    original = str(task.get("prompt") or "").strip()
    current = _effective_run_prompt(task)
    guidance = _guidance_context(task)
    request_bits = [f"Original user request:\n{original or '(none recorded)'}"]
    if current and current != original:
        request_bits.append(f"Current run request:\n{current}")
    if guidance:
        request_bits.append(guidance)
    integration_context = _model_integration_context(task)
    if integration_context:
        request_bits.append(integration_context)
    edit_expectation = ""
    if _request_expects_workspace_edits(task):
        edit_expectation = (
            "This request is fix-oriented. After you identify the concrete root cause, make the smallest viable workspace edit "
            "that addresses it, run a targeted validation step, inspect the resulting diff, and only then finish. "
            "Do not stop at diagnosis alone when a focused fix is available. "
        )
    if text_tool_mode:
        return (
            "You are Nexus Coding Agent in a constrained context window. Work by calling workspace tools, not by narrating. "
            f"{_text_tool_call_guidance()} "
            "Use this loop: inspect files, make focused edits, run the requested validation, inspect coding_git_diff, then coding_finish. "
            "Use small reads: coding_search_text first, then coding_read_file_lines with narrow ranges. "
            "Use coding_replace_text for small edits and coding_run_command for validation. "
            "Never push, open pull requests, or modify files outside the workspace. "
            f"{edit_expectation}"
            f"Allowed commands: {allowed or '(none)'}. "
            f"Workspace task id: {task.get('id')}. Base branch: {task.get('base_branch')}. Working branch: {task.get('branch_name')}.\n\n"
            + "\n\n".join(request_bits)
        )
    return (
        "You are Nexus Coding Agent. Work autonomously toward the user's coding request inside one isolated git workspace. "
        "Use the provided tools to inspect, edit, and test the repository. Do not ask the user for routine next steps. "
        "The coding workspace execution environment is Linux even if the chat UI is running on Windows. "
        "Use POSIX paths and Linux command conventions: forward slashes, ls/cat/mv/cp/rm, python3, VAR=value cmd, and $VAR. "
        "Do not assume PowerShell, cmd.exe, drive letters, backslashes, %VAR%, or $env:VAR inside the workspace. "
        "Treat workspace conversation messages as additional user guidance. If new guidance arrives during a run, adjust during the next work cycle. "
        "Prefer this loop: inspect relevant files, make focused edits, run targeted checks, inspect git diff, then finish. "
        "Keep assistant responses concise; call tools promptly instead of narrating long plans. "
        "Do not push, open pull requests, force-push, rewrite git history, or modify files outside the workspace. "
        "The Gateway may create local checkpoint commits during the run so paused or interrupted work can resume from durable git history. "
        "Call coding_tool_manifest if you need to inspect the exact tools and guidance currently available in this workspace. "
        "Prefer coding_read_file_lines for targeted inspection. Avoid reading full large files unless needed; use coding_search_text first, then focused line ranges. "
        "Prefer coding_replace_text for exact small edits and coding_apply_patch for multi-file diffs; use coding_write_file only for whole-file rewrites or new files. "
        "Before replacing an existing file, read it and preserve unrelated content. Never overwrite a root README or other broad documentation file wholesale unless the user explicitly requested that exact rewrite. "
        "Do not invent imports, variables, functions, methods, classes, settings, or service names. Before using a symbol or API you are not certain exists, search the repository and read the definition or call site. "
        "Keep imports consolidated in the existing style and avoid loading the same library multiple times. "
        "Use coding_fetch_url for public documentation or issue pages when current external information is needed. "
        "Use coding_search_text before reading many files. "
        "Repository-relative commands default to the repo root; when a project layout requires a service-local package root, set cwd to that service directory before running tests or linters. "
        "Never finish by writing a prose-only assistant message; the run only ends when you call coding_finish. "
        "Call coding_finish only after you have either completed the task or identified a concrete blocker. "
        "If the request requires code or documentation changes, make edits, run targeted validation, and inspect coding_git_diff before calling coding_finish. "
        "Do not invent package.json files, lockfiles, requirements files, or placeholder tests just to satisfy validation. Only add project manifests or dependency files when the user explicitly requested that scaffolding or the target service already uses it. "
        "Placeholder handlers or comments such as 'Add logic to ...' do not count as a fix. "
        "If a preferred checker such as pytest or ruff is missing in the workspace, use an available fallback such as python -m py_compile, unittest, node --check, npm test, or git diff --check instead of stopping at the missing tool. "
        "A successful coding_finish after edits will be rejected unless validation and coding_git_diff ran after the latest edit. "
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
        "Execution environment: Linux workspace shell with POSIX paths and Linux-style environment variables.\n"
        "Command cwd defaults to the repo root; switch to a service directory such as services/gateway when that service owns the package/import root for tests or linters.\n"
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
    model = str(requested_model or task.get("coding_model") or "coder").strip() or "coder"
    return model


def _idle_only_huge_model_policy(model: str) -> Optional[Dict[str, Any]]:
    policy = coding_model_policy.describe_workspace_model(model)
    if str(policy.get("run_policy") or "") == "idle_only":
        return policy
    return None


def _settings_for_task_owner(task: Dict[str, Any]) -> Dict[str, Any]:
    user_id = task.get("owner_user_id")
    try:
        if user_id is None:
            return {}
        settings = user_store.get_settings(S.USER_DB_PATH, user_id=int(user_id))
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


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
    running = _active_runner(task_id)
    if running is not None:
        raise HTTPException(status_code=409, detail="coding agent is already running for this workspace")
    persisted_status = str(task.get("agent_status") or "").strip().lower()
    if persisted_status in _ACTIVE_AGENT_STATUSES:
        task = await asyncio.to_thread(_mark_stale_agent_paused, task_id, task)

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
    idle_only_policy = _idle_only_huge_model_policy(model)
    if idle_only_policy:
        summary = str(idle_only_policy.get("warning") or "").strip() or (
            "This workspace is pinned to a huge MLX model that is not currently loaded."
        )
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "coding_model": model,
                "agent_run_id": run_id,
                "agent_status": "idle_waiting",
                "agent_model": model,
                "agent_backend": "",
                "agent_upstream_model": str(idle_only_policy.get("resolved_model") or ""),
                "agent_cycle": 0,
                "agent_started_at": None,
                "agent_finished_at": now,
                "agent_last_event_at": now_unix(),
                "agent_summary": summary,
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
                "type": "idle_deferred",
                "run_id": run_id,
                "model": model,
                "active_huge_model": idle_only_policy.get("active_huge_model") or "",
                "recommended_model": idle_only_policy.get("recommended_model") or "coder",
                "summary": summary,
                "actor": actor or "",
            },
        )
        fresh = await asyncio.to_thread(cw.load_task, task_id)
        return cw.public_task(fresh)

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
    task = await asyncio.to_thread(cw.load_task, task_id)
    running = _active_runner(task_id)
    if running is None:
        status = str(task.get("agent_status") or "").strip().lower()
        if status in _ACTIVE_AGENT_STATUSES:
            task = await asyncio.to_thread(_mark_stale_agent_paused, task_id, task)
        return cw.public_task(task)
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
        user_settings = _settings_for_task_owner(task)
        start_head_result = await asyncio.to_thread(cw.git_head, task_id)
        start_head = str(start_head_result.get("commit") or "")
        route_reason = ""
        if user_llm.is_user_model_id(model):
            parsed = user_llm.parse_user_model_id(model)
            provider, upstream_model = parsed if parsed is not None else ("user", model)
            backend = user_llm.user_backend_name(provider)
            route_reason = "user_llm_settings"
        else:
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
            route_reason = route.reason
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
                "route_reason": route_reason,
            },
        )

        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=_system_prompt(task, text_tool_mode=not _backend_supports_tool_calling(backend))),
            ChatMessage(role="user", content=_task_context(task)),
        ]
        seen_guidance_count = len(_guidance_messages(task))
        tools = _tool_specs()
        no_tool_cycles = 0
        semantic_reroutes = 0
        semantic_failed_backends: set[str] = set()
        workspace_modified = False
        diff_reviewed_after_edit = False
        validation_run_after_edit = False
        validation_ok_after_edit: Optional[bool] = None
        validation_failed_after_edit = False
        diff_result_after_edit: Optional[Dict[str, Any]] = None
        validation_argv_after_edit: Optional[List[str]] = None
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

            request_text_tool_mode = not _backend_supports_tool_calling(backend)
            req = ChatCompletionRequest(
                model=model,
                messages=messages,
                tools=None if request_text_tool_mode else tools,
                tool_choice=None if request_text_tool_mode else "auto",
                temperature=0.1,
                max_tokens=_max_completion_tokens_for_route(model, backend),
                stream=False,
            )
            resp, backend, upstream_model = await _call_backend_chat_with_retry(
                req,
                backend,
                upstream_model,
                task_id=task_id,
                cycle=cycle,
                user_settings=user_settings,
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
            response_text_tool_mode = not _backend_supports_tool_calling(backend)
            event_content = assistant.content if isinstance(assistant.content, str) else ""
            if text_tool_calls and not response_text_tool_mode:
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
                if (
                    not malformed_text_tool_call
                    and not user_llm.is_user_model_id(model)
                    and no_tool_cycles >= 2
                    and semantic_reroutes < _max_semantic_reroutes()
                ):
                    fallback = _semantic_reroute_candidate(
                        model,
                        backend,
                        upstream_model,
                        excluded_backends=semantic_failed_backends | {backend},
                    )
                    if fallback is not None:
                        previous_backend = backend
                        previous_model = upstream_model
                        semantic_failed_backends.add(previous_backend)
                        backend = str(fallback.get("backend") or backend)
                        upstream_model = str(fallback.get("upstream_model") or upstream_model)
                        semantic_reroutes += 1
                        no_tool_cycles = 0
                        reroute_notice = (
                            "The previous coding backend kept returning prose instead of executable workspace tool calls. "
                            f"Rerouting from {previous_backend} to {backend} for the next attempt."
                        )
                        await asyncio.to_thread(
                            _append_event,
                            task_id,
                            {
                                "type": "semantic_reroute",
                                "cycle": cycle,
                                "count": semantic_reroutes,
                                "previous_backend": previous_backend,
                                "previous_upstream_model": previous_model,
                                "backend": backend,
                                "upstream_model": upstream_model,
                                "summary": reroute_notice,
                            },
                        )
                        messages.append(
                            ChatMessage(
                                role="user",
                                content=(
                                    "The previous backend returned prose-only output instead of an executable workspace tool call. "
                                    "Continue the coding task now with exactly one workspace tool call or coding_finish. "
                                    "Do not answer with a prose-only summary."
                                ),
                            )
                        )
                        continue
                if not malformed_text_tool_call and no_tool_cycles >= _max_no_tool_call_cycles():
                    failure_message = (
                        f"The coding model produced {no_tool_cycles} consecutive prose-only responses without an executable workspace tool call. "
                        "Failing this run instead of looping further. Use a different coding model or provide manual guidance before resuming."
                    )
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "no_tool_call_limit",
                            "cycle": cycle,
                            "count": no_tool_cycles,
                            "backend": backend,
                            "upstream_model": upstream_model,
                            "summary": failure_message,
                        },
                    )
                    raise HTTPException(status_code=409, detail=failure_message)
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

                if name == "coding_finish":
                    candidate_success = bool(result.get("success", args.get("success", True)))
                    gate_feedback = _finish_gate_feedback(
                        task=task,
                        finish_success=candidate_success,
                        workspace_modified=workspace_modified,
                        diff_reviewed_after_edit=diff_reviewed_after_edit,
                        validation_run_after_edit=validation_run_after_edit,
                        validation_ok_after_edit=validation_ok_after_edit,
                        validation_failed_after_edit=validation_failed_after_edit,
                        diff_result_after_edit=diff_result_after_edit,
                        validation_argv_after_edit=validation_argv_after_edit,
                    )
                    if gate_feedback:
                        result = {
                            "ok": False,
                            "success": False,
                            "summary": gate_feedback,
                            "error": gate_feedback,
                            "required_tools": ["coding_run_command", "coding_git_diff"],
                        }
                        await asyncio.to_thread(
                            _append_event,
                            task_id,
                            {
                                "type": "finish_gate",
                                "cycle": cycle,
                                "summary": gate_feedback,
                                "diff_reviewed_after_edit": diff_reviewed_after_edit,
                                "validation_run_after_edit": validation_run_after_edit,
                                "validation_ok_after_edit": validation_ok_after_edit,
                                "validation_failed_after_edit": validation_failed_after_edit,
                            },
                        )
                elif _tool_result_modified_workspace(name, args, result):
                    workspace_modified = True
                    diff_reviewed_after_edit = False
                    validation_run_after_edit = False
                    validation_ok_after_edit = None
                    validation_failed_after_edit = False
                    diff_result_after_edit = None
                    validation_argv_after_edit = None
                elif name == "coding_git_diff" and bool(result.get("ok")):
                    diff_reviewed_after_edit = True
                    diff_result_after_edit = result
                elif name == "coding_run_command" and _is_validation_command(args.get("argv")):
                    validation_argv_after_edit = [str(item) for item in (args.get("argv") or []) if str(item)]
                    if _validation_command_failed_due_to_missing_tool(result):
                        validation_run_after_edit = False
                        validation_ok_after_edit = None
                    else:
                        validation_run_after_edit = True
                        validation_ok_after_edit = bool(result.get("ok"))
                        if not validation_ok_after_edit:
                            validation_failed_after_edit = True

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
                if response_text_tool_mode:
                    messages.append(_text_tool_result_message(name=name, result=result))
                else:
                    messages.append(_tool_message_for_result(tool_call_id=tool_call_id, result=result))

                if name == "coding_finish" and bool(result.get("ok")):
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
            expects_workspace_edits=_request_expects_workspace_edits(task),
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
