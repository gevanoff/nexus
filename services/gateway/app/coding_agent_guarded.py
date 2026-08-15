from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional, Sequence

from fastapi import HTTPException

from app import coding_agent as _agent
from app import coding_backend_failover
from app import coding_run_delta
from app import coding_semantic_acceptance
from app import coding_workspace as cw
from app.coding_workspace_reconciliation import reconcile_before_run


_ORIGINAL_CALL_BACKEND_CHAT_WITH_RETRY = getattr(
    _agent,
    "_guarded_original_call_backend_chat_with_retry",
    _agent._call_backend_chat_with_retry,
)
_ORIGINAL_RUN_TOOL = getattr(
    _agent,
    "_guarded_original_run_tool",
    _agent._run_tool,
)
_ORIGINAL_TOOL_RESULT_MODIFIED_WORKSPACE = getattr(
    _agent,
    "_guarded_original_tool_result_modified_workspace",
    _agent._tool_result_modified_workspace,
)


def _candidate_summary(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "backend": candidate.get("backend"),
        "host": candidate.get("host"),
        "ready": bool(candidate.get("ready")),
        "available": int(candidate.get("available") or 0),
        "limit": int(candidate.get("limit") or 0),
        "inflight": int(candidate.get("inflight") or 0),
    }


async def _acquire_backend_excluding(
    request_model: str,
    preferred_backend: str,
    preferred_upstream_model: str,
    *,
    task_id: str,
    cycle: int,
    attempt: int,
    excluded_backends: set[str],
) -> Dict[str, Any]:
    admission = _agent.get_admission_controller()
    deadline = time.monotonic() + _agent._coding_queue_timeout_sec()
    queued_logged = False
    last_candidates: list[Dict[str, Any]] = []
    last_ready_count = 0

    while True:
        ranked = _agent._rank_coding_backend_candidates(
            request_model,
            preferred_backend,
            preferred_upstream_model,
        )
        candidates = [
            dict(item)
            for item in coding_backend_failover.filter_candidates(
                ranked,
                excluded_backends,
            )
        ]
        if not candidates:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "coding_backend_failover_exhausted",
                    "message": (
                        "All coding backends eligible for this request have already "
                        "exhausted a full generation read window"
                    ),
                    "cycle": cycle,
                    "preferred_backend": preferred_backend,
                    "excluded_backends": sorted(excluded_backends),
                    "candidates": [_candidate_summary(item) for item in ranked[:6]],
                },
            )
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
            try:
                if attempt > 0 or str(candidate.get("backend") or "") != preferred_backend:
                    await asyncio.to_thread(
                        _agent._append_event,
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
                            "excluded_backends": sorted(excluded_backends),
                            "summary": (
                                f"selected {candidate.get('backend')} on {candidate.get('host')} "
                                f"(preferred {preferred_backend})"
                            ),
                        },
                    )
            except BaseException:
                admission.release(str(candidate["backend"]), "chat")
                raise
            return candidate

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            status_code = 429 if last_ready_count > 0 else 503
            raise HTTPException(
                status_code=status_code,
                detail={
                    "error": (
                        "coding_backend_queue_timeout"
                        if status_code == 429
                        else "coding_backend_unavailable"
                    ),
                    "message": (
                        "No healthy coding backend became available before the queue "
                        "timeout elapsed"
                    ),
                    "cycle": cycle,
                    "preferred_backend": preferred_backend,
                    "excluded_backends": sorted(excluded_backends),
                    "candidates": [
                        _candidate_summary(item) for item in last_candidates[:6]
                    ],
                },
                headers=(
                    {"Retry-After": str(max(1, int(_agent._coding_queue_poll_sec())))}
                    if status_code == 429
                    else None
                ),
            )
        if not queued_logged:
            await asyncio.to_thread(
                _agent._append_event,
                task_id,
                {
                    "type": "backend_wait",
                    "cycle": cycle,
                    "attempt": attempt + 1,
                    "timeout_sec": round(_agent._coding_queue_timeout_sec(), 1),
                    "preferred_backend": preferred_backend,
                    "excluded_backends": sorted(excluded_backends),
                    "candidates": [
                        _candidate_summary(item) for item in last_candidates[:6]
                    ],
                },
            )
            queued_logged = True
        await asyncio.sleep(
            min(_agent._coding_queue_poll_sec(), max(0.05, remaining))
        )


async def _call_backend_chat_with_failover(
    req: Any,
    backend: str,
    upstream_model: str,
    *,
    task_id: str,
    cycle: int,
    user_settings: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], str, str]:
    if _agent.user_llm.is_user_model_id(req.model):
        return await _ORIGINAL_CALL_BACKEND_CHAT_WITH_RETRY(
            req,
            backend,
            upstream_model,
            task_id=task_id,
            cycle=cycle,
            user_settings=user_settings,
        )

    max_retries = _agent._backend_retry_count()
    admission = _agent.get_admission_controller()
    excluded_backends: set[str] = set()

    for attempt in range(max_retries + 1):
        selected = await _acquire_backend_excluding(
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
            resp = await _agent.call_backend_chat(
                req,
                selected_backend,
                selected_model,
            )
            return resp, selected_backend, selected_model
        except HTTPException as exc:
            if attempt >= max_retries or not _agent._is_retryable_backend_error(exc):
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
            delay = 0.0 if full_read_failover else _agent._backend_retry_delay(attempt)
            detail = coding_backend_failover.backend_error_detail(exc)
            event_type = "backend_failover" if full_read_failover else "backend_retry"
            await asyncio.to_thread(
                _agent._append_event,
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
                    "error": _agent._clip_text(str(detail), 1200),
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


def _current_run_has_recorded_mutation(task: Dict[str, Any]) -> bool:
    run_id = str(task.get("agent_run_id") or "").strip()
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    start_index = 0
    if run_id:
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if (
                str(event.get("type") or "") == "started"
                and str(event.get("run_id") or "") == run_id
            ):
                start_index = index
                break
    edit_tools = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}
    for event in events[start_index:]:
        if str(event.get("type") or "") != "tool_finished":
            continue
        name = str(event.get("name") or "")
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if name in edit_tools and result.get("ok") is True:
            return True
        if name == "coding_run_command" and result.get("workspace_modified") is True:
            return True
    return False


def _project_hypothesis_text(task: Dict[str, Any]) -> str:
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), dict) else {}
    parts = [str(plan.get("note") or "").strip()]
    for item in (plan.get("items") or []):
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if summary:
            parts.append(
                f"{item.get('id') or ''} {item.get('title') or ''}: {summary}".strip()
            )
    return _agent._clip_text("\n".join(part for part in parts if part), 6000)


def _run_delta_diff(task_id: str, task: Dict[str, Any]) -> str:
    return coding_run_delta.run_delta_diff(cw, _agent, task_id, task)


def _deterministic_acceptance_ready(task_id: str) -> bool:
    try:
        snapshot = cw.coding_state_snapshot(task_id)
    except Exception:
        return False
    validation = (
        snapshot.get("validation")
        if isinstance(snapshot.get("validation"), dict)
        else {}
    )
    review = (
        snapshot.get("diff_review")
        if isinstance(snapshot.get("diff_review"), dict)
        else {}
    )
    return bool(validation.get("validation_after_latest_edit")) and bool(
        review.get("diff_reviewed_after_latest_edit")
    )


async def _semantic_acceptance_review(
    task_id: str,
    task: Dict[str, Any],
    *,
    diff_text: str,
) -> Dict[str, Any]:
    model = str(task.get("agent_model") or task.get("coding_model") or "coder")
    backend = str(task.get("agent_backend") or "")
    upstream_model = str(task.get("agent_upstream_model") or "")
    review_backend = backend
    review_model = upstream_model

    if not _agent.user_llm.is_user_model_id(model):
        alternate = _agent._semantic_reroute_candidate(
            model,
            backend,
            upstream_model,
            excluded_backends={backend},
        )
        if alternate is not None:
            review_backend = str(alternate.get("backend") or backend)
            review_model = str(alternate.get("upstream_model") or upstream_model)

    original_request = str(task.get("prompt") or "").strip()
    current_request = _agent._effective_run_prompt(task)
    system_text, user_text = coding_semantic_acceptance.build_review_messages(
        original_request=original_request,
        current_request=current_request,
        hypothesis=_project_hypothesis_text(task),
        diff_text=diff_text,
    )
    req = _agent.ChatCompletionRequest(
        model=model,
        messages=[
            _agent.ChatMessage(role="system", content=system_text),
            _agent.ChatMessage(role="user", content=user_text),
        ],
        tools=None,
        temperature=0.0,
        max_tokens=min(
            1200,
            _agent._max_completion_tokens_for_route(
                model,
                review_backend,
                review_model,
            ),
        ),
        stream=False,
    )
    try:
        resp, selected_backend, selected_model = await _call_backend_chat_with_failover(
            req,
            review_backend,
            review_model,
            task_id=task_id,
            cycle=int(task.get("agent_cycle") or 0),
            user_settings=_agent._settings_for_task_owner(task),
        )
        assistant = _agent._extract_assistant_message(resp)
        review = coding_semantic_acceptance.parse_review(assistant.content)
        review["backend"] = selected_backend
        review["upstream_model"] = selected_model
        return review
    except Exception as exc:
        return {
            "accepted": False,
            "reason": (
                "semantic acceptance reviewer failed: "
                f"{type(exc).__name__}: {exc}"
            ),
            "causal_alignment": False,
            "existing_mechanism_checked": False,
            "acceptance_criteria_checked": False,
            "review_error": True,
        }


def _workspace_progress_fingerprint(task_id: str) -> str:
    try:
        return str(cw.workspace_progress_fingerprint(task_id) or "")
    except Exception:
        return ""


def _tool_result_modified_workspace(
    name: str,
    args: Dict[str, Any],
    result: Dict[str, Any],
) -> bool:
    if result.get("workspace_modified") is True:
        return True
    return _ORIGINAL_TOOL_RESULT_MODIFIED_WORKSPACE(name, args, result)


def _run_tool_with_semantic_acceptance(
    task_id: str,
    name: str,
    args: Dict[str, Any],
    *,
    git_token_value: Optional[str],
) -> Dict[str, Any]:
    mutation_capable = name in {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_run_command",
    }
    before_fingerprint = ""
    if mutation_capable:
        before_edit = cw.load_task(task_id)
        coding_run_delta.ensure_baseline(cw, task_id, before_edit)
        if name == "coding_run_command":
            before_fingerprint = _workspace_progress_fingerprint(task_id)

    result = _ORIGINAL_RUN_TOOL(
        task_id,
        name,
        args,
        git_token_value=git_token_value,
    )
    if name == "coding_run_command" and before_fingerprint:
        after_fingerprint = _workspace_progress_fingerprint(task_id)
        if after_fingerprint and after_fingerprint != before_fingerprint:
            result = dict(result)
            result["workspace_modified"] = True

    if (
        name != "coding_finish"
        or result.get("ok") is not True
        or result.get("success") is not True
    ):
        return result

    task = cw.load_task(task_id)
    diff_text = _run_delta_diff(task_id, task)
    if not diff_text:
        if _current_run_has_recorded_mutation(task):
            return {
                "ok": False,
                "success": False,
                "error": "semantic_acceptance_missing_diff",
                "summary": (
                    "Independent acceptance review could not reconstruct the actual "
                    "run delta. Inspect coding_git_diff and preserve a reviewable diff "
                    "before finishing."
                ),
            }
        return result
    if not _deterministic_acceptance_ready(task_id):
        return result

    review = asyncio.run(
        _semantic_acceptance_review(
            task_id,
            task,
            diff_text=diff_text,
        )
    )
    _agent._append_event(
        task_id,
        {
            "type": "semantic_acceptance_review",
            "cycle": int(task.get("agent_cycle") or 0),
            "accepted": bool(review.get("accepted")),
            "reason": str(review.get("reason") or ""),
            "causal_alignment": bool(review.get("causal_alignment")),
            "existing_mechanism_checked": bool(
                review.get("existing_mechanism_checked")
            ),
            "acceptance_criteria_checked": bool(
                review.get("acceptance_criteria_checked")
            ),
            "backend": str(review.get("backend") or ""),
            "upstream_model": str(review.get("upstream_model") or ""),
        },
    )
    if bool(review.get("accepted")):
        return result
    reason = str(review.get("reason") or "").strip() or (
        "the patch does not yet demonstrate causal alignment with the request"
    )
    return {
        "ok": False,
        "success": False,
        "error": "semantic_acceptance_rejected",
        "summary": (
            f"Independent semantic acceptance review rejected this finish: {reason}. "
            "Revise the patch or its evidence, rerun validation, inspect the diff, "
            "and finish again."
        ),
        "semantic_review": review,
    }


def _install_runtime_policies() -> None:
    if bool(getattr(_agent, "_guarded_runtime_policies_installed", False)):
        return
    _agent._guarded_original_call_backend_chat_with_retry = (
        _ORIGINAL_CALL_BACKEND_CHAT_WITH_RETRY
    )
    _agent._guarded_original_run_tool = _ORIGINAL_RUN_TOOL
    _agent._guarded_original_tool_result_modified_workspace = (
        _ORIGINAL_TOOL_RESULT_MODIFIED_WORKSPACE
    )
    _agent._call_backend_chat_with_retry = _call_backend_chat_with_failover
    _agent._run_tool = _run_tool_with_semantic_acceptance
    _agent._tool_result_modified_workspace = _tool_result_modified_workspace
    _agent._guarded_runtime_policies_installed = True


_install_runtime_policies()


async def _start_original(
    task_id: str,
    *,
    git_token_value: Optional[str],
    coding_model: Optional[str],
    prompt: Optional[str],
    auto_commit: bool,
    commit_message: Optional[str],
    actor: Optional[str],
    max_cycles: Optional[int],
    max_runtime_sec: Optional[int],
    context_reset_cycles: Optional[int],
    mission_overrides: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return await _agent.start_agent_run(
        task_id,
        git_token_value=git_token_value,
        coding_model=coding_model,
        prompt=prompt,
        auto_commit=auto_commit,
        commit_message=commit_message,
        actor=actor,
        max_cycles=max_cycles,
        max_runtime_sec=max_runtime_sec,
        context_reset_cycles=context_reset_cycles,
        mission_overrides=mission_overrides,
    )


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
    task = await asyncio.to_thread(cw.load_task, task_id)
    if _agent._active_runner(task_id) is not None:
        return await _start_original(
            task_id,
            git_token_value=git_token_value,
            coding_model=coding_model,
            prompt=prompt,
            auto_commit=auto_commit,
            commit_message=commit_message,
            actor=actor,
            max_cycles=max_cycles,
            max_runtime_sec=max_runtime_sec,
            context_reset_cycles=context_reset_cycles,
            mission_overrides=mission_overrides,
        )

    persisted_status = str(task.get("agent_status") or "").strip().lower()
    if persisted_status in _agent._ACTIVE_AGENT_STATUSES:
        task = await asyncio.to_thread(_agent._mark_stale_agent_paused, task_id, task)

    reconciliation = await reconcile_before_run(
        task_id,
        git_token_value=git_token_value,
        actor=str(actor or "coding-agent"),
    )
    if not reconciliation.get("proceed", True):
        return cw.public_task(reconciliation["task"])
    return await _start_original(
        task_id,
        git_token_value=git_token_value,
        coding_model=coding_model,
        prompt=prompt,
        auto_commit=auto_commit,
        commit_message=commit_message,
        actor=actor,
        max_cycles=max_cycles,
        max_runtime_sec=max_runtime_sec,
        context_reset_cycles=context_reset_cycles,
        mission_overrides=mission_overrides,
    )


async def resume_interrupted_agent_runs(task_ids: Sequence[str]) -> Dict[str, Any]:
    resumable = []
    integrated = []
    failures: Dict[str, str] = {}
    for raw_task_id in task_ids:
        task_id = str(raw_task_id or "").strip()
        if not task_id:
            continue
        try:
            task = cw.load_task(task_id)
            reconciliation = await reconcile_before_run(
                task_id,
                git_token_value=_agent._git_token_for_task_owner(task),
                actor="gateway-recovery",
            )
            if reconciliation.get("proceed", True):
                resumable.append(task_id)
            else:
                integrated.append(task_id)
        except Exception as exc:
            failures[task_id] = f"{type(exc).__name__}: {exc}"
    resumed = await _agent.resume_interrupted_agent_runs(resumable) if resumable else {
        "ok": True,
        "resumed": 0,
        "tasks": [],
        "failures": {},
    }
    combined_failures = dict(resumed.get("failures") or {})
    combined_failures.update(failures)
    return {
        "ok": not combined_failures,
        "resumed": int(resumed.get("resumed") or 0),
        "tasks": list(resumed.get("tasks") or []),
        "integrated": integrated,
        "failures": combined_failures,
    }


def __getattr__(name: str) -> Any:
    return getattr(_agent, name)