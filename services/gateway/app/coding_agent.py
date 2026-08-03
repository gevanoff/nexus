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
from app import context_budget
from app import coding_semantic_memory
from app import coding_workspace as cw
from app.coding_runtime_guardrails import (
    ProgressDecision,
    ProgressObservation,
    ProgressState,
    evaluate_cycle_progress,
    progress_state_from_dict,
    progress_state_to_dict,
)
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
    def __init__(
        self,
        summary: str,
        *,
        reason_code: str = "manual_or_unspecified_pause",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(summary)
        self.reason_code = reason_code
        self.details = dict(details or {})


_RUNNING: Dict[str, asyncio.Task[Any]] = {}
_ACTIVE_AGENT_STATUSES = {"queued", "running", "stopping", "pausing"}


def _max_events() -> int:
    try:
        return max(20, min(int(getattr(S, "CODING_AGENT_MAX_EVENTS", 1000) or 1000), 1000))
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
    finished_at = time.time()

    def apply(current: Dict[str, Any]) -> None:
        current.update({
            "agent_status": "paused",
            "agent_stop_requested": False,
            "agent_pause_requested": False,
            "agent_summary": summary,
            "agent_error": "",
            "agent_stop_reason_code": "gateway_restart",
            "agent_finished_at": finished_at,
            "agent_last_event_at": now_unix(),
        })

    task = _task_transaction(task_id, apply)
    _append_event(
        task_id,
        {
            "type": "stale_agent_recovered",
            "stop_reason_code": "gateway_restart",
            "previous_status": previous_status,
            "summary": summary,
        },
    )
    run_id = str(task.get("agent_run_id") or "")
    if run_id:
        _update_run_record(
            task_id,
            run_id,
            {
                "status": "paused",
                "finished_at": finished_at,
                "cycle": int(task.get("agent_cycle") or 0),
                "summary": summary,
                "stop_reason_code": "gateway_restart",
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
        try:
            cap = max(128, min(int(alias.max_tokens_cap), 32_768))
        except Exception:
            pass
    if not _backend_supports_tool_calling(backend):
        cap = min(cap, _text_tool_max_completion_tokens())
    return cap


def _tool_context_char_limit() -> int:
    try:
        return max(2_000, min(int(getattr(S, "CODING_AGENT_TOOL_CONTEXT_CHARS", 12_000) or 12_000), 12_000))
    except Exception:
        return 12_000


def _max_cycles_per_run(value: Optional[int] = None) -> int:
    default = int(getattr(S, "CODING_AGENT_MAX_CYCLES_PER_RUN", 1000) or 1000)
    requested = default if value is None else int(value)
    return max(4, min(requested, 1000))


def _max_runtime_sec(value: Optional[int] = None) -> int:
    default = int(getattr(S, "CODING_AGENT_MAX_RUNTIME_SEC", 6 * 60 * 60) or (6 * 60 * 60))
    requested = default if value is None else int(value)
    return max(60, min(requested, 24 * 60 * 60))


def _context_reset_cycles(value: Optional[int] = None) -> int:
    default = int(getattr(S, "CODING_AGENT_CONTEXT_RESET_CYCLES", 0) or 0)
    requested = default if value is None else int(value)
    if requested <= 0:
        return 0
    return max(4, min(requested, 100))


def _context_reset_chars(value: Optional[int] = None) -> int:
    try:
        configured = int(getattr(S, "CODING_AGENT_CONTEXT_RESET_CHARS", 64_000) or 64_000) if value is None else int(value)
        # The local GLM route rejects requests near 98k input characters. Keep
        # enough headroom for the system prompt and the next model response even
        # when an older persisted mission still contains the former 200k value.
        return max(20_000, min(configured, 64_000))
    except Exception:
        return 64_000


def _context_reset_tokens(value: Optional[int] = None, *, model: str = "") -> int:
    alias = get_aliases().get(str(model or "").strip().lower())
    alias_limit = getattr(alias, "coding_context_reset_tokens", None) if alias is not None else None
    if isinstance(alias_limit, int) and alias_limit > 0:
        return max(8_000, alias_limit)
    return max(8_000, context_budget.estimate_char_budget_tokens(_context_reset_chars(value)))


def _mission_for_task(task: Dict[str, Any]) -> Dict[str, Any]:
    return cw.normalize_coding_mission(task)


def _state_read_signature(name: str, args: Dict[str, Any]) -> str:
    if name in {"coding_git_diff", "coding_git_status"}:
        return name
    if name in {"coding_read_file", "coding_read_file_lines"}:
        # The budget is intentionally per file, not per line range. Counting
        # each slightly different range as a new read lets a model reread the
        # same file indefinitely without ever receiving the inspection-loop
        # guidance.
        return f"coding_read_file:{str(args.get('path') or '').strip()}"
    return ""


def _repeated_state_read_decision(count: int, maximum: int) -> str:
    # Inspection limits are coaching thresholds, not independent run budgets.
    # The explicit cycle and wall-clock budgets are the only normal horizons.
    if count >= maximum:
        return "guide"
    return "continue"


def _cancelled_run_status(task: Dict[str, Any]) -> str:
    if bool(task.get("agent_pause_requested") or task.get("agent_stop_requested")):
        return "paused"
    return "interrupted"


def finalize_successful_run(
    task_id: str,
    *,
    mission: Optional[Dict[str, Any]] = None,
    git_token_value: Optional[str] = None,
    finish_summary: str = "",
    run_id: str = "",
) -> Dict[str, Any]:
    """Deterministically commit and optionally publish a successful coding run."""
    task = cw.load_task(task_id)
    contract = cw.normalize_coding_mission(task, mission)
    publish = contract["publish_policy"]
    completion = contract["completion_policy"]
    now = time.time()
    result: Dict[str, Any] = {
        "ok": False,
        "finalization_status": "running",
        "final_commit": "",
        "committed_at": None,
        "pushed_at": None,
        "push_result": None,
        "pr_url": "",
        "pr_number": None,
        "pr_created_at": None,
        "finalization_error": "",
    }
    try:
        status = cw.git_status(task_id, git_token_value=git_token_value)
        diff = cw.git_diff(task_id)
        changes = cw.git_change_summary(task_id)
        snapshot = cw.coding_state_snapshot(task_id)
        before = cw.git_head(task_id)
        if not status.get("ok") or not diff.get("ok") or not changes.get("ok"):
            raise RuntimeError("final repository audit failed")
        change_counts = changes.get("counts") if isinstance(changes.get("counts"), dict) else {}
        has_uncommitted = int(change_counts.get("total") or 0) > 0
        base_changes = diff.get("changes") if isinstance(diff.get("changes"), dict) else {}
        base_counts = base_changes.get("counts") if isinstance(base_changes.get("counts"), dict) else {}
        if completion.get("require_file_changes", True) and int(base_counts.get("total") or 0) <= 0:
            raise RuntimeError("successful run has no meaningful delta versus the base branch")
        if completion.get("require_validation_after_edit", True) and not bool((snapshot.get("validation") or {}).get("validation_after_latest_edit")):
            raise RuntimeError("successful run lacks validation after the latest edit")
        if completion.get("require_diff_review_after_edit", True) and not bool((snapshot.get("diff_review") or {}).get("diff_reviewed_after_latest_edit")):
            raise RuntimeError("successful run lacks diff review after the latest edit")
        if has_uncommitted:
            message = str(finish_summary or "Apply Nexus coding agent changes").strip().splitlines()[0][:160]
            commit = cw.commit_task(task_id, message=message or "Apply Nexus coding agent changes")
            if not commit.get("ok"):
                raise RuntimeError(str(commit.get("error") or "git commit failed"))
            result["final_commit"] = str(commit.get("last_commit") or "")
            result["committed_at"] = time.time()
        else:
            latest = cw.load_task(task_id)
            after = cw.git_head(task_id)
            candidate = str(after.get("commit") or latest.get("last_checkpoint_commit") or latest.get("last_commit") or "")
            start_head = str(latest.get("agent_start_head") or "")
            checkpoint_for_run = str(latest.get("last_checkpoint_run_id") or "") == str(run_id or "")
            if completion.get("require_file_changes", True) and not candidate:
                raise RuntimeError("successful run has no branch commit")
            if completion.get("require_file_changes", True) and start_head and candidate == start_head and not checkpoint_for_run:
                raise RuntimeError("successful run produced no commit after run start")
            result["final_commit"] = candidate
            result["committed_at"] = float(latest.get("last_checkpoint_at") or latest.get("updated_at") or now)
        if completion.get("require_commit_on_success", True) and not result["final_commit"]:
            raise RuntimeError("successful run has no final commit")
        if publish.get("push") == "on_success" or publish.get("draft_pr") == "on_success":
            push = cw.push_task(task_id, remote=publish.get("remote") or "origin", git_token_value=git_token_value)
            result["push_result"] = push
            if not push.get("ok"):
                result["finalization_status"] = "failed_publish"
                raise RuntimeError(str(push.get("stderr") or push.get("error") or "push failed"))
            result["pushed_at"] = time.time()
        if publish.get("draft_pr") == "on_success":
            pr = cw.create_pull_request(
                task_id,
                title=str(publish.get("pr_title") or finish_summary or "Nexus coding mission").splitlines()[0][:200],
                body=str(publish.get("pr_body") or finish_summary or ""),
                draft=True,
                git_token_value=git_token_value,
            )
            if not pr.get("ok"):
                result["finalization_status"] = "failed_publish"
                raise RuntimeError(str(pr.get("error") or "draft PR creation failed"))
            result["pr_url"] = str(pr.get("url") or pr.get("html_url") or pr.get("stdout") or "").strip()
            result["pr_number"] = pr.get("number")
            result["pr_created_at"] = time.time()
        result["ok"] = True
        result["finalization_status"] = "completed"
    except Exception as exc:
        if result["finalization_status"] == "running":
            result["finalization_status"] = "failed_finalization"
        result["finalization_error"] = f"{type(exc).__name__}: {exc}"
    result["stop_reason_code"] = (
        "run_completed"
        if result["finalization_status"] == "completed"
        else str(result["finalization_status"] or "failed_finalization")
    )
    result["finished_at"] = time.time()
    cw.mutate_task(task_id, lambda latest: latest.update({"mission": contract, "terminal_result": result, **result}))
    return result


def _run_history_limit() -> int:
    try:
        return max(10, min(int(getattr(S, "CODING_AGENT_RUN_HISTORY_LIMIT", 50) or 50), 200))
    except Exception:
        return 50


def _messages_char_count(messages: Sequence[ChatMessage]) -> int:
    total = 0
    for message in messages:
        try:
            total += len(json.dumps(message.model_dump(exclude_none=True), ensure_ascii=False))
        except Exception:
            total += len(str(message.content or ""))
    return total


def _messages_token_count(
    messages: Sequence[ChatMessage],
    *,
    tools: Optional[Sequence[ToolSpec]] = None,
) -> int:
    return context_budget.estimate_chat_tokens(messages, tools=tools)


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
    if event_type == "plan_updated":
        return f"plan_updated revision={event.get('revision') or ''} {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
    if event_type == "context_reset":
        return f"context_reset cycle={event.get('cycle') or ''} reason={event.get('reason') or ''}".strip()
    if event_type == "budget_exhausted":
        return f"budget_exhausted cycle={event.get('cycle') or ''} {_clip_text(str(event.get('summary') or '').strip(), 700)}".strip()
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
    bits.append(
        "Resume from the authoritative controller snapshot, durable project plan, current workspace files, "
        "git diff, and checkpoint history. Do not restart repository orientation or repeat completed inspection; "
        "inspect only the state needed for the next unresolved action."
    )
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


def _project_plan_context(task: Dict[str, Any]) -> str:
    plan = cw.normalize_project_plan(task.get("project_plan"), fallback_goal=str(task.get("prompt") or ""))
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    if not items:
        return (
            "Long-horizon project plan:\n"
            f"- Goal: {plan.get('goal') or task.get('prompt') or '(not set)'}\n"
            "- No milestones recorded yet. For work spanning several steps, call coding_update_plan before broad implementation."
        )
    lines = ["Long-horizon project plan:", f"- Goal: {plan.get('goal') or '(not set)'}"]
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "pending")
        summary = str(item.get("summary") or "").strip()
        lines.append(f"- [{status}] {item.get('id') or ''}: {item.get('title') or ''}{f' — {summary}' if summary else ''}")
    if plan.get("note"):
        lines.append(f"- Plan note: {_clip_text(str(plan.get('note') or ''), 1200)}")
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
    compact = _clip_jsonable(result, min(_tool_context_char_limit(), 1000))
    payload = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
    return ChatMessage(
        role="user",
        content=(
            f"Tool result for {name}:\n{payload}\n\n"
            "Continue the coding task with exactly one complete <tool_call>{...}</tool_call> block, "
            "or call coding_finish when the task is complete or blocked."
        ),
    )


def _compact_text_tool_messages(messages: List[ChatMessage]) -> List[ChatMessage]:
    if len(messages) <= 8:
        return messages
    head = messages[:2]
    tail = messages[-5:]
    return [
        *head,
        ChatMessage(
            role="user",
            content=(
                "Earlier text-tool call history was omitted to stay within this backend's small context window. "
                "Use the recent tool results below, avoid repeating completed inspection, and continue with one <tool_call>{...}</tool_call> block."
            ),
        ),
        *tail,
    ]


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
    if "argv" not in out and isinstance(out.get("command"), list):
        out["argv"] = out.get("command")
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
                name="coding_update_plan",
                description=(
                    "Create or replace the durable project milestone plan for this workspace. "
                    "Use it for multi-step work and update milestone statuses as the run progresses."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "items": {
                            "type": "array",
                            "maxItems": 80,
                            "items": {
                                "type": "object",
                                "required": ["id", "title", "status"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed", "blocked", "skipped"],
                                    },
                                    "summary": {"type": "string"},
                                },
                            },
                        },
                        "note": {"type": "string"},
                    },
                },
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
        "For work spanning several milestones, use coding_update_plan and keep milestone statuses current.",
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


def _task_transaction(task_id: str, mutator: Any) -> Dict[str, Any]:
    try:
        return cw.mutate_task(task_id, mutator)
    except HTTPException as exc:
        # Preserve the load/save seam used by in-memory adapters and focused
        # tests while keeping real file-backed updates atomic.
        if int(getattr(exc, "status_code", 0) or 0) != 404:
            raise
        task = cw.load_task(task_id)
        mutator(task)
        cw.save_task(task)
        return task


def _mutate_task(task_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    return _task_transaction(task_id, lambda task: task.update(fields))


def _progress_observation(
    task_id: str,
    *,
    cycle: int,
    validation_revision: int,
    diff_review_revision: int,
    finish_state: str,
    workspace_fingerprint: Optional[str] = None,
) -> ProgressObservation:
    task = cw.load_task(task_id)
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), dict) else {}
    return ProgressObservation(
        cycle=cycle,
        workspace_fingerprint=workspace_fingerprint or cw.workspace_progress_fingerprint(task_id),
        plan_revision=int(plan.get("revision") or 0),
        validation_revision=max(0, int(validation_revision)),
        diff_review_revision=max(0, int(diff_review_revision)),
        finish_state=str(finish_state or "running"),
        guidance_revision=float(task.get("last_guidance_at") or 0),
    )


def _initialize_cycle_progress(
    task_id: str,
    run_id: str,
    observation: ProgressObservation,
) -> ProgressState:
    result: Dict[str, ProgressState] = {}

    def apply(task: Dict[str, Any]) -> None:
        existing = progress_state_from_dict(task.get("agent_progress_state"))
        if existing.observation is not None:
            result["state"] = existing
            return
        state = ProgressState(observation=observation, stagnant_cycles=0)
        task["agent_progress_state"] = progress_state_to_dict(state)
        task.setdefault(
            "agent_last_successful_validation_fingerprint",
            observation.workspace_fingerprint,
        )
        task.setdefault(
            "agent_last_diff_review_fingerprint",
            observation.workspace_fingerprint,
        )
        runs = task.get("agent_runs") if isinstance(task.get("agent_runs"), list) else []
        for run in reversed(runs):
            if isinstance(run, dict) and str(run.get("run_id") or "") == run_id:
                run["cycle"] = observation.cycle
                run["stagnant_cycles"] = 0
                break
        result["state"] = state

    _task_transaction(task_id, apply)
    return result["state"]


def _record_cycle_progress(
    task_id: str,
    run_id: str,
    observation: ProgressObservation,
    *,
    max_stagnant_cycles: int,
    validated_fingerprint: str = "",
    reviewed_fingerprint: str = "",
) -> ProgressDecision:
    result: Dict[str, ProgressDecision] = {}

    def apply(task: Dict[str, Any]) -> None:
        previous = progress_state_from_dict(task.get("agent_progress_state"))
        decision = evaluate_cycle_progress(
            previous,
            observation,
            max_stagnant_cycles=max_stagnant_cycles,
        )
        task["agent_progress_state"] = progress_state_to_dict(decision.state)
        task["agent_cycle"] = observation.cycle
        if validated_fingerprint:
            task["agent_last_successful_validation_fingerprint"] = validated_fingerprint
        if reviewed_fingerprint:
            task["agent_last_diff_review_fingerprint"] = reviewed_fingerprint
        runs = task.get("agent_runs") if isinstance(task.get("agent_runs"), list) else []
        for run in reversed(runs):
            if isinstance(run, dict) and str(run.get("run_id") or "") == run_id:
                run["cycle"] = observation.cycle
                run["stagnant_cycles"] = decision.state.stagnant_cycles
                break
        result["decision"] = decision

    _task_transaction(task_id, apply)
    return result["decision"]


def _append_event(task_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}

    def apply(task: Dict[str, Any]) -> None:
        events = task.get("agent_events")
        if not isinstance(events, list):
            events = []
        ev = _compact_event(task, {"ts": now_unix(), **event})
        events.append(ev)
        task["agent_events"] = events[-_max_events():]
        task["agent_last_event_at"] = ev["ts"]
        result.update(ev)

    _task_transaction(task_id, apply)
    return result


def _update_run_record(task_id: str, run_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    def apply(task: Dict[str, Any]) -> None:
        runs = task.get("agent_runs")
        if not isinstance(runs, list):
            runs = []
        record: Optional[Dict[str, Any]] = None
        for item in reversed(runs):
            if isinstance(item, dict) and str(item.get("run_id") or "") == run_id:
                record = item
                break
        if record is None:
            record = {"run_id": run_id, "created_at": time.time()}
            runs.append(record)
        record.update(fields)
        task["agent_runs"] = [item for item in runs[-_run_history_limit():] if isinstance(item, dict)]

    return _task_transaction(task_id, apply)


def _initialize_run_state(
    task_id: str,
    *,
    fields: Dict[str, Any],
    run_record: Dict[str, Any],
    requested_prompt: str,
    actor: Optional[str],
) -> Dict[str, Any]:
    """Start a run without replacing guidance, events, or history added concurrently."""

    def apply(task: Dict[str, Any]) -> None:
        events = task.get("agent_events")
        if not isinstance(events, list):
            events = []
        runs = task.get("agent_runs")
        if not isinstance(runs, list):
            runs = []
        run_id = str(run_record.get("run_id") or "")
        runs = [item for item in runs if isinstance(item, dict) and str(item.get("run_id") or "") != run_id]
        runs.append(dict(run_record))

        guidance = _guidance_messages(task)
        if requested_prompt:
            guidance_ts = time.time()
            guidance.append(
                {
                    "ts": guidance_ts,
                    "role": "user",
                    "actor": str(actor or "").strip(),
                    "run_id": run_id,
                    "content": requested_prompt,
                }
            )
            task["last_guidance_at"] = guidance_ts

        task.update(fields)
        task["agent_events"] = events[-_max_events():]
        task["agent_runs"] = runs[-_run_history_limit():]
        task["guidance_messages"] = guidance[-200:]

    return _task_transaction(task_id, apply)


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
    if name == "coding_update_plan":
        if not any(key in args for key in ("goal", "items", "note")):
            return {"ok": False, "error": "coding_update_plan requires goal, items, or note"}
        items = args.get("items")
        if items is not None and not isinstance(items, list):
            return {"ok": False, "error": "items must be an array"}
        result = cw.update_project_plan(
            task_id,
            goal=str(args.get("goal") or "") if "goal" in args else None,
            items=items,
            note=str(args.get("note") or "") if "note" in args else None,
            actor="coding-agent",
        )
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        return {"ok": True, "plan": plan}
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
    request_bits.append(_project_plan_context(task))
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
            "For multi-step work, create and maintain the durable project plan with coding_update_plan. "
            "Use small reads: coding_search_text first, then coding_read_file_lines with narrow ranges. "
            "Use coding_replace_text for small edits and coding_run_command for validation. "
            "Do not push or open pull requests directly. Nexus will commit successful work and optionally push/open a draft PR after your successful coding_finish according to the mission contract. "
            "Do not modify files outside the workspace. "
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
        "For work spanning several milestones, call coding_update_plan early, keep exactly one relevant item in_progress, and update completed or blocked items as evidence changes. "
        "Prefer this loop: inspect relevant files, make focused edits, run targeted checks, inspect git diff, then finish. "
        "Keep assistant responses concise; call tools promptly instead of narrating long plans. "
        "Do not push or open pull requests directly. Nexus will commit successful work and optionally push/open a draft PR after your successful coding_finish according to the mission contract. "
        "Do not force-push, rewrite git history, or modify files outside the workspace. "
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
    is_continuation = bool(str(task.get("agent_previous_run_id") or "").strip())
    opening = (
        "Resume at the next unresolved action from the durable state below. Do not start over or repeat completed "
        "repository inspection."
        if is_continuation
        else "Start by inspecting the repository, then proceed without waiting for more user input."
    )
    base = (
        f"Original user request:\n{original}\n\n"
        f"Current run request:\n{current or original}\n\n"
        f"Repository: {cw.redact_repo_url(str(task.get('repo_url') or ''))}\n"
        f"Branch: {task.get('branch_name')} from {task.get('base_branch')}\n"
        "Execution environment: Linux workspace shell with POSIX paths and Linux-style environment variables.\n"
        "Command cwd defaults to the repo root; switch to a service directory such as services/gateway when that service owns the package/import root for tests or linters.\n"
        f"{opening}"
    )
    guidance = _guidance_context(task)
    if guidance:
        base = f"{base}\n\n{guidance}"
    base = f"{base}\n\n{_project_plan_context(task)}"
    try:
        snapshot = cw.coding_state_snapshot(str(task.get("id") or ""))
        # Guidance is already rendered above and recent run events are rendered
        # by _previous_run_context. Repeating both full collections inside the
        # snapshot made a freshly compacted GLM prompt larger than the 64k
        # compaction threshold, causing another reset on every cycle.
        snapshot.pop("recent_guidance", None)
        snapshot.pop("recent_events", None)
        base = f"{base}\n\nController state snapshot (authoritative):\n{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}"
    except Exception:
        pass
    previous = _previous_run_context(task)
    if previous:
        return f"{base}\n\n{previous}"
    return base


def _text_tool_task_context(task: Dict[str, Any]) -> str:
    return (
        "Start now using the request, repository, branch, and allowed-command details in the system message. "
        "Emit exactly one complete <tool_call>{...}</tool_call> block."
    )


def _choose_model(task: Dict[str, Any], requested_model: Optional[str]) -> str:
    model = str(requested_model or task.get("coding_model") or "coder").strip() or "coder"
    return model


def _effective_max_no_progress_cycles(
    progress_policy: Dict[str, Any],
    *,
    backend: str,
    upstream_model: str,
) -> int:
    """Apply a longer hard pause to the slow resident GLM coding route."""
    configured = max(2, int(progress_policy.get("max_no_progress_cycles") or 8))
    normalized_backend = str(backend or "").strip().lower()
    normalized_model = str(upstream_model or "").strip().lower()
    if normalized_backend == "local_mlx" and "glm-5.2" in normalized_model:
        long_model_limit = max(
            2,
            int(progress_policy.get("long_model_max_no_progress_cycles") or 12),
        )
        return max(configured, long_model_limit)
    return configured


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


def _git_token_for_task_owner(task: Dict[str, Any]) -> str:
    settings = _settings_for_task_owner(task)
    coding = settings.get("coding") if isinstance(settings.get("coding"), dict) else {}
    return str(coding.get("git_token") or "").strip()


async def resume_interrupted_agent_runs(task_ids: Sequence[str]) -> Dict[str, Any]:
    """Resume runs interrupted by a Gateway restart in the new event loop."""

    if not bool(getattr(S, "CODING_AGENT_AUTO_RESUME_INTERRUPTED", True)):
        return {"ok": True, "resumed": 0, "tasks": [], "failures": {}}
    resumed: List[str] = []
    failures: Dict[str, str] = {}
    for raw_task_id in task_ids:
        task_id = str(raw_task_id or "").strip()
        if not task_id:
            continue
        try:
            task = await asyncio.to_thread(cw.load_task, task_id)
            if str(task.get("agent_status") or "").strip().lower() != "interrupted":
                continue
            await start_agent_run(
                task_id,
                git_token_value=_git_token_for_task_owner(task),
                coding_model=str(task.get("coding_model") or task.get("agent_model") or "coder"),
                auto_commit=bool(task.get("agent_auto_commit")),
                actor="gateway-recovery",
                max_cycles=int(task.get("agent_max_cycles") or _max_cycles_per_run()),
                max_runtime_sec=int(task.get("agent_max_runtime_sec") or _max_runtime_sec()),
                context_reset_cycles=int(task.get("agent_context_reset_cycles") or 0),
            )
            resumed.append(task_id)
        except Exception as exc:
            failures[task_id] = f"{type(exc).__name__}: {exc}"
    return {"ok": not failures, "resumed": len(resumed), "tasks": resumed, "failures": failures}


async def start_agent_run(
    task_id: str,
    *,
    git_token_value: Optional[str] = None,
    coding_model: Optional[str] = None,
    prompt: Optional[str] = None,
    auto_commit: bool = False,
    commit_message: Optional[str] = None,
    actor: Optional[str] = None,
    max_cycles: Optional[int] = None,
    max_runtime_sec: Optional[int] = None,
    context_reset_cycles: Optional[int] = None,
    mission_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cw._ensure_enabled()
    task = await asyncio.to_thread(cw.load_task, task_id)
    if mission_overrides:
        mission = cw.normalize_coding_mission(task, mission_overrides)
        task = await asyncio.to_thread(cw.mutate_task, task_id, lambda latest: latest.update({"mission": mission}))
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
    previous_stop_reason_code = str(task.get("agent_stop_reason_code") or "")
    requested_prompt = str(prompt or "").strip()
    effective_prompt = requested_prompt or str(task.get("prompt") or "").strip()
    now = time.time()
    run_max_cycles = _max_cycles_per_run(max_cycles)
    run_max_runtime_sec = _max_runtime_sec(max_runtime_sec)
    run_context_reset_cycles = _context_reset_cycles(context_reset_cycles)
    run_record = {
        "run_id": run_id,
        "status": "queued",
        "created_at": now,
        "started_at": now,
        "finished_at": None,
        "model": model,
        "backend": "",
        "upstream_model": "",
        "cycle": 0,
        "max_cycles": run_max_cycles,
        "max_runtime_sec": run_max_runtime_sec,
        "context_reset_cycles": run_context_reset_cycles,
        "prompt": _clip_text(effective_prompt, 1_000),
        "actor": str(actor or "").strip(),
        "continuation": bool(previous_run_id or previous_events),
        "summary": "",
        "error": "",
        "stop_reason_code": "",
    }
    idle_only_policy = _idle_only_huge_model_policy(model)
    if idle_only_policy:
        summary = str(idle_only_policy.get("warning") or "").strip() or (
            "This workspace is pinned to a huge MLX model that is not currently loaded."
        )
        await asyncio.to_thread(
            _initialize_run_state,
            task_id,
            fields={
                "coding_model": model,
                "agent_run_id": run_id,
                "agent_status": "idle_waiting",
                "agent_model": model,
                "agent_backend": "",
                "agent_upstream_model": str(idle_only_policy.get("resolved_model") or ""),
                "agent_cycle": 0,
                "agent_max_cycles": run_max_cycles,
                "agent_max_runtime_sec": run_max_runtime_sec,
                "agent_context_reset_cycles": run_context_reset_cycles,
                "agent_started_at": None,
                "agent_finished_at": now,
                "agent_last_event_at": now_unix(),
                "agent_summary": summary,
                "agent_error": "",
                "agent_stop_reason_code": "idle_waiting",
                "agent_previous_run_id": previous_run_id,
                "agent_previous_status": previous_status,
                "agent_previous_summary": previous_summary,
                "agent_previous_error": previous_error,
                "agent_previous_stop_reason_code": previous_stop_reason_code,
                "agent_stop_requested": False,
                "agent_pause_requested": False,
                "agent_auto_resume_pending": False,
                "agent_auto_commit": bool(auto_commit),
                "agent_run_prompt": effective_prompt,
            },
            run_record=run_record,
            requested_prompt=requested_prompt,
            actor=actor,
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
                "stop_reason_code": "idle_waiting",
                "actor": actor or "",
            },
        )
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "status": "idle_waiting",
                "finished_at": now,
                "summary": summary,
                "stop_reason_code": "idle_waiting",
            },
        )
        fresh = await asyncio.to_thread(cw.load_task, task_id)
        return cw.public_task(fresh)

    await asyncio.to_thread(
        _initialize_run_state,
        task_id,
        fields={
            "coding_model": model,
            "agent_run_id": run_id,
            "agent_status": "queued",
            "agent_model": model,
            "agent_backend": "",
            "agent_upstream_model": "",
            "agent_cycle": 0,
            "agent_max_cycles": run_max_cycles,
            "agent_max_runtime_sec": run_max_runtime_sec,
            "agent_context_reset_cycles": run_context_reset_cycles,
            "agent_started_at": now,
            "agent_finished_at": None,
            "agent_last_event_at": now_unix(),
            "agent_summary": "",
            "agent_error": "",
            "agent_stop_reason_code": "",
            "agent_previous_run_id": previous_run_id,
            "agent_previous_status": previous_status,
            "agent_previous_summary": previous_summary,
            "agent_previous_error": previous_error,
            "agent_previous_stop_reason_code": previous_stop_reason_code,
            "agent_stop_requested": False,
            "agent_pause_requested": False,
            "agent_auto_resume_pending": False,
            "agent_auto_commit": bool(auto_commit),
            "agent_run_prompt": effective_prompt,
        },
        run_record=run_record,
        requested_prompt=requested_prompt,
        actor=actor,
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
            max_cycles=run_max_cycles,
            max_runtime_sec=run_max_runtime_sec,
            context_reset_cycles=run_context_reset_cycles,
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


async def create_and_start_agent_run(
    *,
    repo_url: Optional[str],
    base_branch: Optional[str],
    branch_name: Optional[str],
    prompt: Optional[str],
    owner: str,
    owner_user_id: Optional[int] = None,
    git_token_value: Optional[str] = None,
    coding_model: Optional[str] = None,
    commit_message: Optional[str] = None,
    actor: Optional[str] = None,
    max_cycles: Optional[int] = None,
    max_runtime_sec: Optional[int] = None,
    context_reset_cycles: Optional[int] = None,
    mission_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Canonical create-and-run service used by REST, UI, and model tools."""
    task = await asyncio.to_thread(
        cw.create_task,
        repo_url=repo_url,
        base_branch=base_branch,
        branch_name=branch_name,
        prompt=prompt,
        owner=owner,
        owner_user_id=owner_user_id,
        git_token_value=git_token_value,
        coding_model=coding_model,
        mission_overrides=mission_overrides,
    )
    if task.get("status") == "error":
        return task
    return await start_agent_run(
        str(task.get("id") or ""),
        git_token_value=git_token_value,
        coding_model=coding_model,
        auto_commit=True,
        commit_message=commit_message,
        actor=actor or owner,
        max_cycles=max_cycles,
        max_runtime_sec=max_runtime_sec,
        context_reset_cycles=context_reset_cycles,
        mission_overrides=mission_overrides,
    )


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


async def _enforce_cycle_progress_decision(
    task_id: str,
    *,
    cycle: int,
    decision: ProgressDecision,
) -> None:
    checkpoint_injected = False
    try:
        checkpoint_injected = bool(
            await asyncio.to_thread(coding_semantic_memory.process_task, task_id)
        )
    except Exception as exc:
        logger.warning(
            "coding semantic checkpoint failed task_id=%s cycle=%s (%s: %s)",
            task_id,
            cycle,
            type(exc).__name__,
            exc,
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "investigation_checkpoint_error",
                "cycle": cycle,
                "summary": _clip_text(f"{type(exc).__name__}: {exc}", 1000),
            },
        )

    fresh_checkpoint = checkpoint_injected
    if decision.pause and not fresh_checkpoint:
        try:
            latest = await asyncio.to_thread(cw.load_task, task_id)
            checkpoint = (
                latest.get("agent_investigation_checkpoint")
                if isinstance(latest.get("agent_investigation_checkpoint"), dict)
                else {}
            )
            fresh_checkpoint = (
                int(checkpoint.get("cycle") or 0) == cycle
                and str(checkpoint.get("run_id") or "")
                == str(latest.get("agent_run_id") or "")
            )
        except Exception as exc:
            logger.warning(
                "coding semantic checkpoint freshness check failed task_id=%s cycle=%s (%s: %s)",
                task_id,
                cycle,
                type(exc).__name__,
                exc,
            )

    if fresh_checkpoint and decision.pause:
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "no_progress_recovery",
                "cycle": cycle,
                "stagnant_cycles": decision.state.stagnant_cycles,
                "summary": (
                    "A durable investigation checkpoint was injected at the no-progress boundary. "
                    "Granting one bounded recovery transition so the model can act on the required next action."
                ),
            },
        )
        return

    if not decision.pause:
        return

    await asyncio.to_thread(
        _append_event,
        task_id,
        {
            "type": "no_progress_limit",
            "cycle": cycle,
            "reason_code": decision.reason_code,
            "summary": decision.summary,
            "stagnant_cycles": decision.state.stagnant_cycles,
        },
    )
    raise _CodingAgentPaused(
        decision.summary,
        reason_code=decision.reason_code,
        details={
            "cycle": cycle,
            "stagnant_cycles": decision.state.stagnant_cycles,
        },
    )


async def _run_agent(
    task_id: str,
    *,
    run_id: str,
    git_token_value: Optional[str],
    model: str,
    auto_commit: bool,
    commit_message: Optional[str],
    max_cycles: int,
    max_runtime_sec: int,
    context_reset_cycles: int,
) -> None:
    t0 = time.monotonic()
    finish_summary = ""
    finish_success = False
    finish_called = False
    backend = ""
    upstream_model = ""
    cycle = 0
    try:
        task = await asyncio.to_thread(cw.load_task, task_id)
        user_settings = _settings_for_task_owner(task)
        start_head_result = await asyncio.to_thread(cw.git_head, task_id)
        start_head = str(start_head_result.get("commit") or "")
        await asyncio.to_thread(_mutate_task, task_id, {"agent_start_head": start_head})
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
        latest_task = await asyncio.to_thread(cw.load_task, task_id)
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "status": "running",
                "backend": backend,
                "upstream_model": upstream_model,
                "started_at": time.time(),
            },
        )

        initial_text_tool_mode = not _backend_supports_tool_calling(backend)
        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=_system_prompt(task, text_tool_mode=initial_text_tool_mode)),
            ChatMessage(role="user", content=_text_tool_task_context(task) if initial_text_tool_mode else _task_context(task)),
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
        state_read_counts: Dict[str, int] = {}
        active_mission = _mission_for_task(task)
        progress_policy = active_mission.get("budget_policy") or {}
        context_policy = active_mission.get("context_policy") or {}
        max_repeated_state_reads = int(progress_policy.get("max_repeated_state_reads") or 6)
        max_repeated_same_file_reads = int(progress_policy.get("max_repeated_same_file_reads") or 4)
        max_no_progress_cycles = _effective_max_no_progress_cycles(
            progress_policy,
            backend=backend,
            upstream_model=upstream_model,
        )
        recovery_checkpoint_cycles = max(
            2,
            min(
                int(
                    progress_policy.get("recovery_checkpoint_cycles")
                    or min(8, max_no_progress_cycles)
                ),
                max_no_progress_cycles,
            ),
        )
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_effective_max_no_progress_cycles": max_no_progress_cycles,
                "agent_recovery_checkpoint_cycles": recovery_checkpoint_cycles,
            },
        )
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "max_no_progress_cycles": max_no_progress_cycles,
                "recovery_checkpoint_cycles": recovery_checkpoint_cycles,
            },
        )
        context_reset_chars = _context_reset_chars(context_policy.get("context_reset_chars"))
        context_reset_tokens = _context_reset_tokens(
            context_policy.get("context_reset_chars"),
            model=model,
        )
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_context_reset_tokens": context_reset_tokens,
                "agent_context_reset_chars_fallback": context_reset_chars,
                "agent_context_token_estimator": context_budget.TOKEN_ESTIMATOR_NAME,
            },
        )
        persisted_progress = progress_state_from_dict(task.get("agent_progress_state"))
        prior_observation = persisted_progress.observation
        validation_revision = prior_observation.validation_revision if prior_observation is not None else 0
        diff_review_revision = prior_observation.diff_review_revision if prior_observation is not None else 0
        baseline = await asyncio.to_thread(
            _progress_observation,
            task_id,
            cycle=0,
            validation_revision=validation_revision,
            diff_review_revision=diff_review_revision,
            finish_state="running",
        )
        await asyncio.to_thread(
            _initialize_cycle_progress,
            task_id,
            run_id,
            baseline,
        )
        while True:
            elapsed_sec = time.monotonic() - t0
            if cycle >= max_cycles or elapsed_sec >= max_runtime_sec:
                reason = "cycle budget" if cycle >= max_cycles else "wall-clock budget"
                budget_summary = (
                    f"Coding run paused after reaching its {reason} at cycle {cycle}. "
                    "Workspace files, the durable project plan, and checkpoint commits are preserved. "
                    "Start a continuation run to keep working."
                )
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "budget_exhausted",
                        "cycle": cycle,
                        "elapsed_runtime_sec": int(elapsed_sec),
                        "max_cycles": max_cycles,
                        "max_runtime_sec": max_runtime_sec,
                        "reason": reason.replace(" ", "_"),
                        "reason_code": "cycle_budget" if cycle >= max_cycles else "wall_clock_budget",
                        "summary": budget_summary,
                    },
                )
                raise _CodingAgentPaused(
                    budget_summary,
                    reason_code="cycle_budget" if cycle >= max_cycles else "wall_clock_budget",
                    details={"cycle": cycle, "elapsed_runtime_sec": int(elapsed_sec)},
                )
            cycle += 1
            _raise_if_paused(task_id)
            await asyncio.to_thread(_mutate_task, task_id, {"agent_cycle": cycle, "agent_last_event_at": now_unix()})
            await asyncio.to_thread(_update_run_record, task_id, run_id, {"cycle": cycle})
            await asyncio.to_thread(_append_event, task_id, {"type": "cycle_started", "cycle": cycle})
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
            context_chars = _messages_char_count(messages)
            request_text_tool_mode = not _backend_supports_tool_calling(backend)
            context_tokens = _messages_token_count(
                messages,
                tools=None if request_text_tool_mode else tools,
            )
            completion_reserve_tokens = _max_completion_tokens_for_route(model, backend)
            reset_for_cycles = context_reset_cycles > 0 and cycle > 1 and (cycle - 1) % context_reset_cycles == 0
            reset_for_size = cycle > 1 and context_tokens >= context_reset_tokens
            if reset_for_cycles or reset_for_size:
                latest_task = await asyncio.to_thread(cw.load_task, task_id)
                request_text_tool_mode = not _backend_supports_tool_calling(backend)
                messages = [
                    ChatMessage(role="system", content=_system_prompt(latest_task, text_tool_mode=request_text_tool_mode)),
                    ChatMessage(
                        role="user",
                        content=(
                            (
                                _text_tool_task_context(latest_task)
                                if request_text_tool_mode
                                else _task_context(latest_task)
                            )
                            + "\n\n"
                            + f"Continue the same coding run at cycle {cycle}. Use the controller-provided state snapshot as authoritative. "
                            + "Only re-open files or re-run diff/status if the snapshot is stale, incomplete, or directly relevant to the next edit. "
                            + "Continue with the snapshot's next recommended action."
                        ),
                    ),
                ]
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "context_reset",
                        "cycle": cycle,
                        "reason": "cycle_interval" if reset_for_cycles else "context_size",
                        "previous_context_chars": context_chars,
                        "summary": "Agent conversation context compacted from durable workspace state.",
                    },
                )
            request_text_tool_mode = not _backend_supports_tool_calling(backend)
            request_messages = _compact_text_tool_messages(messages) if request_text_tool_mode else messages
            req = ChatCompletionRequest(
                model=model,
                messages=request_messages,
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
            cycle_validation_succeeded = False
            cycle_diff_reviewed = False
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

                state_read = _state_read_signature(name, args)
                repeated_state_reads = 0
                if state_read:
                    repeated_state_reads = state_read_counts.get(state_read, 0) + 1
                    state_read_counts[state_read] = repeated_state_reads
                repeated_limit = max_repeated_same_file_reads if state_read.startswith("coding_read_file") else max_repeated_state_reads
                progress_decision = _repeated_state_read_decision(repeated_state_reads, repeated_limit)
                if progress_decision == "guide":
                    guidance = "The same repository state read is repeating without progress. Trust the controller snapshot and take the next edit, validation, review, or finish action."
                    messages.append(ChatMessage(role="user", content=guidance))
                    await asyncio.to_thread(_append_event, task_id, {"type": "no_progress_guidance", "cycle": cycle, "summary": guidance, "signature": state_read})

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
                elif name == "coding_update_plan" and bool(result.get("ok")):
                    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
                    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "plan_updated",
                            "cycle": cycle,
                            "revision": plan.get("revision"),
                            "summary": (
                                f"Project plan updated: {counts.get('done', 0)}/{counts.get('total', 0)} milestones done."
                            ),
                        },
                    )
                elif _tool_result_modified_workspace(name, args, result):
                    workspace_modified = True
                    cycle_validation_succeeded = False
                    cycle_diff_reviewed = False
                    diff_reviewed_after_edit = False
                    validation_run_after_edit = False
                    validation_ok_after_edit = None
                    validation_failed_after_edit = False
                    diff_result_after_edit = None
                    validation_argv_after_edit = None
                elif name == "coding_git_diff" and bool(result.get("ok")):
                    diff_reviewed_after_edit = True
                    diff_result_after_edit = result
                    cycle_diff_reviewed = True
                elif name == "coding_run_command" and _is_validation_command(args.get("argv")):
                    validation_argv_after_edit = [str(item) for item in (args.get("argv") or []) if str(item)]
                    if _validation_command_failed_due_to_missing_tool(result):
                        validation_run_after_edit = False
                        validation_ok_after_edit = None
                    else:
                        validation_run_after_edit = True
                        validation_ok_after_edit = bool(result.get("ok"))
                        if validation_ok_after_edit:
                            cycle_validation_succeeded = True
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

            current_fingerprint = await asyncio.to_thread(
                cw.workspace_progress_fingerprint,
                task_id,
            )
            task_after_cycle = await asyncio.to_thread(cw.load_task, task_id)
            validated_fingerprint = ""
            reviewed_fingerprint = ""
            if (
                cycle_validation_succeeded
                and current_fingerprint
                != str(task_after_cycle.get("agent_last_successful_validation_fingerprint") or "")
            ):
                validation_revision += 1
                validated_fingerprint = current_fingerprint
            if (
                cycle_diff_reviewed
                and current_fingerprint
                != str(task_after_cycle.get("agent_last_diff_review_fingerprint") or "")
            ):
                diff_review_revision += 1
                reviewed_fingerprint = current_fingerprint
            observation = await asyncio.to_thread(
                _progress_observation,
                task_id,
                cycle=cycle,
                validation_revision=validation_revision,
                diff_review_revision=diff_review_revision,
                finish_state="finished" if finish_called else "running",
                workspace_fingerprint=current_fingerprint,
            )
            decision = await asyncio.to_thread(
                _record_cycle_progress,
                task_id,
                run_id,
                observation,
                max_stagnant_cycles=max_no_progress_cycles,
                validated_fingerprint=validated_fingerprint,
                reviewed_fingerprint=reviewed_fingerprint,
            )
            await asyncio.to_thread(
                _append_event,
                task_id,
                {
                    "type": "cycle_progress",
                    "cycle": cycle,
                    "progressed": decision.progressed,
                    "stagnant_cycles": decision.state.stagnant_cycles,
                },
            )
            await _enforce_cycle_progress_decision(
                task_id,
                cycle=cycle,
                decision=decision,
            )
            if decision.progressed:
                state_read_counts.clear()

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

        if finish_success:
            finalization = await asyncio.to_thread(
                finalize_successful_run,
                task_id,
                mission=_mission_for_task(await asyncio.to_thread(cw.load_task, task_id)),
                git_token_value=git_token_value,
                finish_summary=str(commit_message or finish_summary or "Apply Nexus coding agent changes"),
                run_id=run_id,
            )
            await asyncio.to_thread(_append_event, task_id, {"type": "finalization", "result": _event_result(finalization), **finalization})
            if not finalization.get("ok"):
                finish_success = False
                finish_summary = f"Code work completed but controller finalization failed: {finalization.get('finalization_error') or 'unknown error'}"

        finished_at = time.time()
        latest_after_finalization = await asyncio.to_thread(cw.load_task, task_id)
        finalization_status = str(latest_after_finalization.get("finalization_status") or "")
        final_status = "completed" if finish_success else (finalization_status or "failed")
        final_stop_reason_code = (
            "run_completed"
            if finish_success
            else finalization_status
            if finalization_status in {"failed_finalization", "failed_publish"}
            else "agent_failed"
        )
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": final_status,
                "agent_summary": finish_summary,
                "agent_error": "" if finish_success else finish_summary,
                "agent_stop_reason_code": final_stop_reason_code,
                "agent_finished_at": finished_at,
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "status": final_status,
                "finished_at": finished_at,
                "cycle": cycle,
                "backend": backend,
                "upstream_model": upstream_model,
                "summary": finish_summary,
                "error": "" if finish_success else finish_summary,
                "stop_reason_code": final_stop_reason_code,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
                "commit": str(latest_task.get("last_commit") or ""),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "completed" if finish_success else "failed",
                "ok": finish_success,
                "stop_reason_code": final_stop_reason_code,
                "summary": finish_summary,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
    except asyncio.CancelledError:
        finished_at = time.time()
        latest_cancelled_task = await asyncio.to_thread(cw.load_task, task_id)
        cancelled_status = _cancelled_run_status(latest_cancelled_task)
        user_requested = cancelled_status == "paused"
        cancelled_reason_code = "manual_or_unspecified_pause" if user_requested else "gateway_stopped"
        cancelled_summary = (
            "Coding run was paused by request. Start another run on this workspace to resume from the latest files and checkpoint."
            if user_requested
            else "Gateway stopped while this coding run was active. Nexus will automatically resume it from durable workspace state."
        )
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": cancelled_status,
                "agent_error": "",
                "agent_summary": cancelled_summary,
                "agent_stop_reason_code": cancelled_reason_code,
                "agent_finished_at": finished_at,
                "agent_last_event_at": now_unix(),
                "agent_auto_resume_pending": not user_requested,
                "agent_stop_requested": False,
                "agent_pause_requested": False,
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "paused" if user_requested else "interrupted",
                "summary": cancelled_summary,
                "stop_reason_code": cancelled_reason_code,
            },
        )
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "status": cancelled_status,
                "finished_at": finished_at,
                "cycle": cycle,
                "backend": backend,
                "upstream_model": upstream_model,
                "summary": cancelled_summary,
                "stop_reason_code": cancelled_reason_code,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
        raise
    except _CodingAgentPaused as exc:
        finished_at = time.time()
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "paused",
                "agent_error": "",
                "agent_summary": str(exc),
                "agent_stop_reason_code": exc.reason_code,
                "agent_finished_at": finished_at,
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "paused",
                "summary": str(exc),
                "stop_reason_code": exc.reason_code,
                "details": exc.details,
            },
        )
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "status": "paused",
                "finished_at": finished_at,
                "cycle": cycle,
                "backend": backend,
                "upstream_model": upstream_model,
                "summary": str(exc),
                "stop_reason_code": exc.reason_code,
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
    except Exception as exc:
        error = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        finished_at = time.time()
        logger.warning("coding agent failed task=%s backend=%s model=%s error=%s", task_id, backend, upstream_model, error)
        await asyncio.to_thread(
            _mutate_task,
            task_id,
            {
                "agent_status": "failed",
                "agent_error": str(error),
                "agent_summary": "",
                "agent_stop_reason_code": "agent_failed",
                "agent_finished_at": finished_at,
                "agent_last_event_at": now_unix(),
            },
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "failed",
                "stop_reason_code": "agent_failed",
                "error": _clip_text(str(error), 4000),
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
        await asyncio.to_thread(
            _update_run_record,
            task_id,
            run_id,
            {
                "status": "failed",
                "finished_at": finished_at,
                "cycle": cycle,
                "backend": backend,
                "upstream_model": upstream_model,
                "error": _clip_text(str(error), 4_000),
                "stop_reason_code": "agent_failed",
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 1),
            },
        )
