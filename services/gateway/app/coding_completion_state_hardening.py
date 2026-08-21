from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException


_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
_CONSUMED_STATUS = "consumed"
_MUTATION_TOOLS = {
    "coding_write_file",
    "coding_replace_text",
    "coding_apply_patch",
    "coding_run_command",
}
_PROTOCOL_ERROR = "coding_backend_protocol_invariant"


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
    effective_task = dispatch.coding_execution_policy.execution_task(agent, task)
    fresh_system = agent._system_prompt(effective_task, text_tool_mode=True)
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


def _finish_gate_overrides(
    cw: Any,
    task: Mapping[str, Any],
    kwargs: Mapping[str, Any],
) -> Dict[str, Any]:
    updated = dict(kwargs)
    durable_validation, durable_review = _durable_acceptance_state(cw, task)
    validation_run = bool(updated.get("validation_run_after_edit"))
    validation_ok = updated.get("validation_ok_after_edit")
    if (validation_run and validation_ok is True) or durable_validation:
        updated["validation_run_after_edit"] = True
        updated["validation_ok_after_edit"] = True
        updated["validation_failed_after_edit"] = False
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
    if getattr(guarded, "_run_tool_with_semantic_acceptance", None) is original_run_tool:
        guarded._run_tool_with_semantic_acceptance = run_tool_with_hypothesis_lifecycle

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
