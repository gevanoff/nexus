from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException

from app import coding_work_phases


_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
_CONSUMED_STATUS = "consumed"
_MUTATION_TOOLS = {
    "coding_write_file",
    "coding_replace_text",
    "coding_apply_patch",
    "coding_run_command",
}
_PROTOCOL_ERROR = "coding_backend_protocol_invariant"
_MISSING_TOOL_MARKERS = (
    "command not found:",
    "no module named pytest",
    "no module named ruff",
    "no module named mypy",
    "modulenotfounderror: no module named",
    "executable file not found",
    "not recognized as an internal or external command",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plan(task: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(task.get("project_plan"))


def _note_fingerprint(note: str) -> str:
    return hashlib.sha256(str(note or "").encode("utf-8")).hexdigest()


def _matching_consumed_lifecycle(task: Mapping[str, Any]) -> Mapping[str, Any]:
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    if str(lifecycle.get("status") or "") != _CONSUMED_STATUS:
        return {}
    plan = _plan(task)
    note = str(plan.get("note") or "")
    if not note.strip():
        return {}
    try:
        plan_revision = int(plan.get("revision") or 0)
        consumed_revision = int(lifecycle.get("plan_revision") or 0)
    except (TypeError, ValueError):
        return {}
    if plan_revision != consumed_revision:
        return {}
    fingerprint = str(lifecycle.get("note_fingerprint") or "")
    if not fingerprint or fingerprint != _note_fingerprint(note):
        return {}
    return lifecycle


def _historical_task(task: Mapping[str, Any]) -> Mapping[str, Any]:
    lifecycle = _matching_consumed_lifecycle(task)
    if not lifecycle:
        return task
    plan = _plan(task)
    historical = dict(task)
    historical_plan = dict(plan)
    original_note = str(plan.get("note") or "").strip()
    historical_plan["note"] = (
        "HISTORICAL CONSUMED REMEDIATION HYPOTHESIS — retained for audit only. "
        "A repository mutation has already consumed this hypothesis. Do not treat it as current "
        "causal truth on continuation or acceptance review without fresh repository evidence and "
        "a new project-plan revision.\n\n"
        + original_note
    )
    historical["project_plan"] = historical_plan
    return historical


def _lifecycle_context(task: Mapping[str, Any]) -> str:
    lifecycle = _matching_consumed_lifecycle(task)
    if not lifecycle:
        return ""
    evidence = str(lifecycle.get("verified_evidence_digest") or "").strip()
    bits = [
        "Hypothesis lifecycle: consumed by a repository mutation.",
        "The recorded remediation hypothesis is historical audit context, not current causal truth.",
        "Revalidate repository evidence before using it to justify another edit or terminal claim.",
    ]
    if evidence:
        bits.extend(
            [
                "",
                "Verified pre-edit repository evidence snapshot:",
                evidence,
            ]
        )
    return "\n".join(bits)


def _protocol_role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or "").strip()
    return str(getattr(message, "role", "") or "").strip()


def _protocol_content(message: Any) -> str:
    if isinstance(message, Mapping):
        value = message.get("content")
    else:
        value = getattr(message, "content", None)
    return value if isinstance(value, str) else ""


def _protocol_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, Mapping):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    return list(calls) if isinstance(calls, list) else []


def _text_tool_protocol_violations(dispatch: Any, req: Any) -> list[str]:
    violations: list[str] = []
    messages = list(dispatch._request_value(req, "messages", None) or [])
    if any(_protocol_role(message) == "tool" for message in messages):
        violations.append("role_tool_message")
    if any(_protocol_tool_calls(message) for message in messages):
        violations.append("native_tool_calls")
    if dispatch._request_value(req, "tools", None) is not None:
        violations.append("native_tools_schema")
    if dispatch._request_value(req, "tool_choice", None) is not None:
        violations.append("native_tool_choice")
    if dispatch._request_value(req, "parallel_tool_calls", None) is not None:
        violations.append("native_parallel_tool_calls")
    return violations


def _repair_text_tool_transport(
    agent: Any,
    dispatch: Any,
    req: Any,
    task: Mapping[str, Any],
) -> tuple[Any, Dict[str, int]]:
    messages = list(dispatch._request_value(req, "messages", None) or [])
    fresh_system = next(
        (
            _protocol_content(message)
            for message in messages
            if _protocol_role(message) == "system" and _protocol_content(message).strip()
        ),
        "",
    )
    if not fresh_system:
        fresh_system = agent._system_prompt(task, text_tool_mode=True)
    normalized, diagnostics = dispatch._normalize_messages(
        agent,
        messages,
        text_tool_mode=True,
        fresh_system=fresh_system,
    )
    repaired = dispatch._copy_request(
        req,
        messages=normalized,
        tools=None,
        tool_choice=None,
        parallel_tool_calls=None,
    )
    return repaired, diagnostics


def _current_run_events(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    events = [event for event in (task.get("agent_events") or []) if isinstance(event, Mapping)]
    run_id = str(task.get("agent_run_id") or "").strip()
    if run_id:
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if (
                str(event.get("type") or "") == "started"
                and str(event.get("run_id") or "").strip() == run_id
            ):
                return events[index:]
        return []
    for index in range(len(events) - 1, -1, -1):
        if str(events[index].get("type") or "") == "started":
            return events[index:]
    return events


def _tool_pairs(task: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]:
    events = _current_run_events(task)
    pending_by_id: dict[str, tuple[int, Mapping[str, Any]]] = {}
    pending_by_name: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    pairs: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    for index, event in enumerate(events):
        event_type = str(event.get("type") or "")
        name = str(event.get("name") or "")
        call_id = str(event.get("tool_call_id") or "")
        if event_type == "tool_started":
            if call_id:
                pending_by_id[call_id] = (index, event)
            else:
                pending_by_name.setdefault(name, []).append((index, event))
            continue
        if event_type != "tool_finished":
            continue
        started: Optional[tuple[int, Mapping[str, Any]]] = None
        if call_id:
            started = pending_by_id.pop(call_id, None)
        if started is None:
            candidates = pending_by_name.get(name) or []
            if candidates:
                started = candidates.pop()
        if started is not None:
            pairs.append((index, started[1], event))
    return pairs


def _result_ok(event: Mapping[str, Any]) -> bool:
    result = _mapping(event.get("result"))
    return result.get("ok") is True


def _result_missing_tool(event: Mapping[str, Any]) -> bool:
    result = _mapping(event.get("result"))
    if result.get("ok") is True:
        return False
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    return any(marker in text for marker in _MISSING_TOOL_MARKERS)


def _event_modified_workspace(
    started: Mapping[str, Any],
    finished: Mapping[str, Any],
) -> bool:
    name = str(started.get("name") or "")
    args = _mapping(started.get("args"))
    result = _mapping(finished.get("result"))
    if result.get("ok") is not True:
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
        apply_result = _mapping(result.get("apply"))
        return apply_result.get("ok") is True if apply_result else True
    if name == "coding_run_command":
        return result.get("workspace_modified") is True
    return False


def _validation_signature(argv: Any) -> tuple[str, ...]:
    if not isinstance(argv, list):
        return ()
    return tuple(str(item).strip() for item in argv if str(item).strip())


def _validation_recovery_state(task: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return (successful_after_latest_edit, all_substantive_failures_superseded).

    A failed validation is superseded only by a later successful invocation of the
    same validation argv after the latest repository mutation. This deliberately
    does not allow a weak checker such as ``git diff --check`` to erase a failed
    pytest/py_compile/lint invocation.
    """

    pairs = _tool_pairs(task)
    latest_mutation_index = -1
    for finished_index, started, finished in pairs:
        if _event_modified_workspace(started, finished):
            latest_mutation_index = max(latest_mutation_index, finished_index)

    validations: list[tuple[int, tuple[str, ...], bool]] = []
    for finished_index, started, finished in pairs:
        if finished_index <= latest_mutation_index:
            continue
        if str(started.get("name") or "") != "coding_run_command":
            continue
        args = _mapping(started.get("args"))
        argv = args.get("argv")
        if not coding_work_phases.is_validation_command(argv):
            continue
        if _result_missing_tool(finished):
            continue
        signature = _validation_signature(argv)
        if not signature:
            continue
        validations.append((finished_index, signature, _result_ok(finished)))

    successes = [record for record in validations if record[2]]
    failures = [record for record in validations if not record[2]]
    if not successes:
        return False, False
    if not failures:
        return True, True
    all_superseded = all(
        any(
            success_index > failure_index and success_signature == failure_signature
            for success_index, success_signature, _ok in successes
        )
        for failure_index, failure_signature, _ok in failures
    )
    return True, all_superseded


def _durable_acceptance_state(cw: Any, task: Mapping[str, Any]) -> tuple[bool, bool]:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return False, False
    try:
        snapshot = cw.coding_state_snapshot(task_id)
    except Exception:
        return False, False
    validation = _mapping(snapshot.get("validation"))
    review = _mapping(snapshot.get("diff_review"))
    validation_ready = bool(validation.get("validation_after_latest_edit")) and bool(
        validation.get("last_validation_ok")
    )
    review_ready = bool(review.get("diff_reviewed_after_latest_edit"))
    return validation_ready, review_ready


def _latest_task(cw: Any, task: Mapping[str, Any]) -> Mapping[str, Any]:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return task
    try:
        latest = cw.load_task(task_id)
    except Exception:
        return task
    return latest if isinstance(latest, Mapping) else task


def _finish_gate_overrides(
    cw: Any,
    task: Mapping[str, Any],
    kwargs: Mapping[str, Any],
) -> Dict[str, Any]:
    updated = dict(kwargs)
    latest_task = _latest_task(cw, task)
    durable_validation, durable_review = _durable_acceptance_state(cw, latest_task)
    validation_run = bool(updated.get("validation_run_after_edit"))
    validation_ok = updated.get("validation_ok_after_edit")
    validation_failed = bool(updated.get("validation_failed_after_edit"))

    successful_after_edit, failures_superseded = _validation_recovery_state(latest_task)
    if validation_failed:
        if failures_superseded and (
            successful_after_edit
            and (durable_validation or (validation_run and validation_ok is True))
        ):
            updated["validation_run_after_edit"] = True
            updated["validation_ok_after_edit"] = True
            updated["validation_failed_after_edit"] = False
    elif durable_validation:
        updated["validation_run_after_edit"] = True
        updated["validation_ok_after_edit"] = True

    if durable_review:
        updated["diff_reviewed_after_edit"] = True
    return updated


def _mutation_succeeded(agent: Any, name: str, args: Dict[str, Any], result: Dict[str, Any]) -> bool:
    if name not in _MUTATION_TOOLS:
        return False
    try:
        return bool(agent._tool_result_modified_workspace(name, args, result))
    except Exception:
        return False


def _verified_evidence_digest(
    persistence: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> str:
    digest = getattr(persistence, "_verified_evidence_digest", None)
    if not callable(digest):
        return ""
    try:
        return str(digest(task, state) or "").strip()
    except Exception:
        return ""


def _record_consumed_hypothesis(
    agent: Any,
    cw: Any,
    persistence: Any,
    *,
    task_id: str,
    before_task: Mapping[str, Any],
    before_state: Mapping[str, Any],
    tool_name: str,
) -> None:
    plan = _plan(before_task)
    note = str(plan.get("note") or "").strip()
    if not note:
        return
    try:
        plan_revision = int(plan.get("revision") or 0)
    except (TypeError, ValueError):
        plan_revision = 0
    try:
        workspace_fingerprint = str(cw.workspace_progress_fingerprint(task_id) or "")
    except Exception:
        workspace_fingerprint = ""
    lifecycle = {
        "schema": "nexus_coding_hypothesis_lifecycle.v1",
        "status": _CONSUMED_STATUS,
        "plan_revision": plan_revision,
        "note_fingerprint": _note_fingerprint(note),
        "consumed_at": time.time(),
        "consumed_run_id": str(before_task.get("agent_run_id") or ""),
        "consumed_by_tool": tool_name,
        "workspace_fingerprint_after": workspace_fingerprint,
        "causal_evidence_targets": list(before_state.get("causal_evidence_targets") or []),
        "causal_evidence_ranges": list(before_state.get("causal_evidence_ranges") or []),
        "verified_evidence_digest": _verified_evidence_digest(
            persistence,
            before_task,
            before_state,
        ),
    }
    agent._mutate_task(task_id, {_LIFECYCLE_KEY: lifecycle})
    agent._append_event(
        task_id,
        {
            "type": "hypothesis_consumed",
            "run_id": lifecycle["consumed_run_id"],
            "plan_revision": plan_revision,
            "tool": tool_name,
            "workspace_fingerprint": workspace_fingerprint,
            "summary": (
                "The current remediation hypothesis was consumed by a repository mutation. "
                "It remains in the project plan for audit history but must be revalidated before "
                "being treated as current causal truth."
            ),
        },
    )


def install(
    agent: Any,
    guarded: Any,
    cw: Any,
    execution_dispatch: Any,
    persistence: Any,
    semantic_acceptance: Any,
) -> None:
    if bool(getattr(agent, "_completion_state_hardening_installed", False)):
        return

    original_finish_gate = agent._finish_gate_feedback

    def finish_gate_feedback(*args: Any, **kwargs: Any) -> str:
        task = kwargs.get("task")
        task_map = task if isinstance(task, Mapping) else {}
        normalized = _finish_gate_overrides(cw, task_map, kwargs)
        return original_finish_gate(*args, **normalized)

    agent._finish_gate_feedback = finish_gate_feedback

    original_run_tool = agent._run_tool

    def run_tool_with_hypothesis_lifecycle(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Optional[str],
    ) -> Dict[str, Any]:
        before_task = cw.load_task(task_id)
        before_state = agent.forced_action.active_state(before_task)
        result = original_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )
        if _mutation_succeeded(agent, name, args, result):
            _record_consumed_hypothesis(
                agent,
                cw,
                persistence,
                task_id=task_id,
                before_task=before_task,
                before_state=before_state,
                tool_name=name,
            )
        return result

    agent._run_tool = run_tool_with_hypothesis_lifecycle

    original_task_context = getattr(agent, "_task_context", None)
    if callable(original_task_context):

        def task_context(task: Mapping[str, Any]) -> str:
            rendered = original_task_context(_historical_task(task))
            lifecycle = _lifecycle_context(task)
            return f"{rendered}\n\n{lifecycle}" if lifecycle else rendered

        agent._task_context = task_context

    original_text_task_context = getattr(agent, "_text_tool_task_context", None)
    if callable(original_text_task_context):

        def text_task_context(task: Mapping[str, Any]) -> str:
            rendered = original_text_task_context(_historical_task(task))
            lifecycle = _lifecycle_context(task)
            return f"{rendered}\n\n{lifecycle}" if lifecycle else rendered

        agent._text_tool_task_context = text_task_context

    original_project_hypothesis = guarded._project_hypothesis_text

    def project_hypothesis_text(task: Mapping[str, Any]) -> str:
        rendered = original_project_hypothesis(_historical_task(task))
        lifecycle = _lifecycle_context(task)
        return f"{lifecycle}\n\n{rendered}".strip() if lifecycle else rendered

    guarded._project_hypothesis_text = project_hypothesis_text

    original_build_review_messages = semantic_acceptance.build_review_messages

    def build_review_messages(**kwargs: Any) -> tuple[str, str]:
        system, user = original_build_review_messages(**kwargs)
        system += (
            " Treat lifecycle labels in the supplied hypothesis literally: a hypothesis marked "
            "historical or consumed is audit history, not established current truth. Set "
            "existing_mechanism_checked=true only when the supplied repository evidence/context "
            "actually supports that the patch preserves intentional specialization and does not "
            "broaden behavior merely to satisfy the recorded hypothesis. Missing evidence is not "
            "evidence of absence."
        )
        return system, user

    semantic_acceptance.build_review_messages = build_review_messages

    original_materialize = execution_dispatch.materialize_request

    def materialize_with_protocol_invariant(
        current_agent: Any,
        req: Any,
        task: Mapping[str, Any],
        *,
        source_backend: str,
        backend: str,
        upstream_model: str,
    ):
        materialized, snapshot, diagnostics = original_materialize(
            current_agent,
            req,
            task,
            source_backend=source_backend,
            backend=backend,
            upstream_model=upstream_model,
        )
        if not bool(getattr(snapshot, "text_tool_mode", False)):
            return materialized, snapshot, diagnostics

        before = _text_tool_protocol_violations(execution_dispatch, materialized)
        repaired, repair_diag = _repair_text_tool_transport(
            current_agent,
            execution_dispatch,
            materialized,
            task,
        )
        after = _text_tool_protocol_violations(execution_dispatch, repaired)
        if after:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": _PROTOCOL_ERROR,
                    "message": (
                        "Refusing to send native tool-protocol history to a text-tool backend."
                    ),
                    "backend": backend,
                    "upstream_model": upstream_model,
                    "violations": after,
                },
            )

        enriched = dict(diagnostics)
        enriched["transport_invariant_repaired"] = bool(before)
        enriched["transport_invariant_repairs"] = list(before)
        for key, value in repair_diag.items():
            if isinstance(value, int):
                enriched[f"transport_{key}"] = int(value)
        return repaired, snapshot, enriched

    execution_dispatch.materialize_request = materialize_with_protocol_invariant
    execution_dispatch._materialize_request_before_completion_state_hardening = original_materialize

    agent._completion_state_hardening_installed = True
