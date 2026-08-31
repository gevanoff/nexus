from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Mapping, Sequence


_SCHEMA = "nexus_coding_semantic_acceptance_state.v1"
_REJECTION_EVENT = "semantic_acceptance_state"
_VALIDATION_SCHEMA = "nexus_coding_validation_provenance.v1"
_VALIDATION_KEY = "coding_validation_provenance"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _semantic_plan_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    plan = _mapping(task.get("project_plan"))
    items = []
    for raw in plan.get("items") or []:
        item = _mapping(raw)
        items.append(
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "status": str(item.get("status") or ""),
                "summary": str(item.get("summary") or ""),
            }
        )
    return {
        "note": str(plan.get("note") or ""),
        "items": items,
    }


def _semantic_evidence_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    forced = _mapping(task.get("agent_forced_action"))
    lifecycle = _mapping(task.get("agent_hypothesis_lifecycle"))
    return {
        "causal_evidence_targets": list(forced.get("causal_evidence_targets") or []),
        "causal_evidence_ranges": list(forced.get("causal_evidence_ranges") or []),
        "hypothesis_ready": bool(forced.get("hypothesis_ready")),
        "targeted_evidence_count": int(forced.get("targeted_evidence_count") or 0),
        "verified_evidence_digest": str(lifecycle.get("verified_evidence_digest") or ""),
        "lifecycle_status": str(lifecycle.get("status") or ""),
    }


def semantic_acceptance_fingerprint(
    task: Mapping[str, Any],
    *,
    diff_text: str,
) -> str:
    """Hash only acceptance-relevant state, excluding cycle/timestamp churn."""
    return _stable_hash(
        {
            "schema": _SCHEMA,
            "diff_sha256": hashlib.sha256(str(diff_text or "").encode("utf-8")).hexdigest(),
            "plan": _semantic_plan_state(task),
            "evidence": _semantic_evidence_state(task),
        }
    )


def _prior_rejection(task: Mapping[str, Any], fingerprint: str) -> Mapping[str, Any]:
    if not fingerprint:
        return {}
    for raw in reversed(list(task.get("agent_events") or [])):
        event = _mapping(raw)
        if str(event.get("type") or "") != _REJECTION_EVENT:
            continue
        if str(event.get("fingerprint") or "") != fingerprint:
            continue
        if event.get("accepted") is False and not bool(event.get("review_error")):
            return event
    return {}


def _record_rejection(
    agent: Any,
    task_id: str,
    task: Mapping[str, Any],
    *,
    fingerprint: str,
    result: Mapping[str, Any],
) -> None:
    review = _mapping(result.get("semantic_review"))
    reason = str(review.get("reason") or result.get("summary") or "").strip()
    agent._append_event(
        task_id,
        {
            "type": _REJECTION_EVENT,
            "schema": _SCHEMA,
            "cycle": int(task.get("agent_cycle") or 0),
            "fingerprint": fingerprint,
            "accepted": False,
            "review_error": bool(review.get("review_error")),
            "reason": reason,
            "summary": (
                "Semantic acceptance rejected this exact diff/hypothesis/evidence state. "
                "A repeat review is blocked until acceptance-relevant state changes."
            ),
        },
    )


def _blocked_repeat_result(previous: Mapping[str, Any]) -> Dict[str, Any]:
    reason = str(previous.get("reason") or "").strip()
    suffix = f" Previous rejection: {reason}" if reason else ""
    return {
        "ok": False,
        "success": False,
        "error": "semantic_acceptance_state_unchanged",
        "required_action": (
            "Change the repository diff or materially revise the remediation hypothesis/evidence, "
            "then rerun validation and diff review before finishing again."
        ),
        "summary": (
            "Independent semantic acceptance already rejected the current diff/hypothesis/evidence "
            "state; the reviewer was not called again. Change acceptance-relevant state before "
            f"retrying coding_finish.{suffix}"
        ),
    }


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _qualifies_validation(argv: Any, is_validation_command: Any) -> bool:
    if not isinstance(argv, (list, tuple)):
        return False
    try:
        return bool(is_validation_command(argv))
    except Exception:
        return False


def _ledger_validation_candidate(
    task: Mapping[str, Any],
    *,
    is_validation_command: Any,
) -> Dict[str, Any]:
    latest: Mapping[str, Any] = {}
    latest_ts = 0.0
    for raw in task.get("commands") or []:
        command = _mapping(raw)
        if str(command.get("label") or "") not in {"agent-command", "command"}:
            continue
        argv = command.get("argv")
        if not _qualifies_validation(argv, is_validation_command):
            continue
        ts = _float(command.get("ts"))
        if not latest or ts >= latest_ts:
            latest = command
            latest_ts = ts
    if not latest:
        return {}
    return {
        "schema": _VALIDATION_SCHEMA,
        "argv": [str(item) for item in (latest.get("argv") or [])],
        "ok": bool(latest.get("ok")),
        "ts": latest_ts,
        "source": "command_ledger",
    }


def _durable_validation_candidate(
    task: Mapping[str, Any],
    *,
    is_validation_command: Any,
) -> Dict[str, Any]:
    durable = _mapping(task.get(_VALIDATION_KEY))
    if str(durable.get("schema") or "") != _VALIDATION_SCHEMA:
        return {}
    argv = durable.get("argv")
    if not _qualifies_validation(argv, is_validation_command):
        return {}
    ts = _float(durable.get("ts"))
    if not ts:
        return {}
    return {
        "schema": _VALIDATION_SCHEMA,
        "argv": [str(item) for item in (argv or [])],
        "ok": bool(durable.get("ok")),
        "ts": ts,
        "cwd": str(durable.get("cwd") or ""),
        "run_id": str(durable.get("run_id") or ""),
        "cycle": int(durable.get("cycle") or 0),
        "source": "durable_provenance",
    }


def _latest_command_timestamp(
    task: Mapping[str, Any],
    *,
    argv: Sequence[str],
) -> float:
    wanted = [str(item) for item in argv]
    for raw in reversed(list(task.get("commands") or [])):
        command = _mapping(raw)
        if str(command.get("label") or "") not in {"agent-command", "command"}:
            continue
        if [str(item) for item in (command.get("argv") or [])] != wanted:
            continue
        ts = _float(command.get("ts"))
        if ts:
            return ts
    return 0.0


def _persist_validation_provenance(
    cw: Any,
    work_phases: Any,
    *,
    task_id: str,
    argv: Any,
    cwd: Any,
    result: Mapping[str, Any],
) -> None:
    if not _qualifies_validation(argv, work_phases.is_validation_command):
        return
    task = cw.load_task(task_id)
    normalized_argv = [str(item) for item in argv]
    ts = _latest_command_timestamp(task, argv=normalized_argv) or time.time()
    task[_VALIDATION_KEY] = {
        "schema": _VALIDATION_SCHEMA,
        "argv": normalized_argv,
        "ok": bool(result.get("ok")),
        "ts": ts,
        "cwd": str(cwd or ""),
        "run_id": str(task.get("agent_run_id") or ""),
        "cycle": int(task.get("agent_cycle") or 0),
    }
    cw.save_task(task)


def _reconciled_validation_state(
    task: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    is_validation_command: Any,
) -> Dict[str, Any]:
    changes = _mapping(snapshot.get("changes"))
    last_edit_at = _float(changes.get("last_edit_at"))

    candidates = [
        candidate
        for candidate in (
            _durable_validation_candidate(
                task,
                is_validation_command=is_validation_command,
            ),
            _ledger_validation_candidate(
                task,
                is_validation_command=is_validation_command,
            ),
        )
        if candidate
    ]
    if not candidates:
        return {
            "last_validation_command": [],
            "last_validation_ok": None,
            "last_validation_at": 0.0,
            "validation_after_latest_edit": False,
            "provenance_source": "none",
        }

    latest = max(candidates, key=lambda item: _float(item.get("ts")))
    latest_ts = _float(latest.get("ts"))
    return {
        "last_validation_command": [str(item) for item in (latest.get("argv") or [])],
        "last_validation_ok": bool(latest.get("ok")),
        "last_validation_at": latest_ts,
        "validation_after_latest_edit": bool(latest_ts and latest_ts >= last_edit_at),
        "provenance_source": str(latest.get("source") or ""),
    }


def _reconciled_progress_state(
    snapshot: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> Dict[str, Any]:
    progress = dict(_mapping(snapshot.get("progress")))
    changes = _mapping(snapshot.get("changes"))
    diff_review = _mapping(snapshot.get("diff_review"))
    changed_files = changes.get("changed_files") if isinstance(changes.get("changed_files"), list) else []

    if not changed_files:
        progress["current_phase"] = "editing"
        progress["next_recommended_action"] = "continue the current project-plan milestone"
        return progress

    if not bool(validation.get("validation_after_latest_edit")):
        progress["current_phase"] = "editing"
        progress["next_recommended_action"] = "validate changes"
        return progress

    if validation.get("last_validation_ok") is not True:
        progress["current_phase"] = "editing"
        progress["next_recommended_action"] = "resolve failed validation"
        return progress

    if not bool(diff_review.get("diff_reviewed_after_latest_edit")):
        progress["current_phase"] = "reviewing"
        progress["next_recommended_action"] = "review diff"
        return progress

    progress["current_phase"] = "finalizing"
    progress["next_recommended_action"] = "finish the mission"
    return progress


def install(agent: Any, guarded: Any, cw: Any, work_phases: Any) -> None:
    """Harden semantic finish retries and durable validation provenance."""
    if bool(getattr(guarded, "_terminal_acceptance_hardening_installed", False)):
        return

    original_run = guarded._run_tool_with_semantic_acceptance

    def run_with_rejection_dedup(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        fingerprint = ""
        task: Mapping[str, Any] = {}
        if name == "coding_finish":
            try:
                task = cw.load_task(task_id)
                diff_text = guarded._run_delta_diff(task_id, dict(task))
                if diff_text:
                    fingerprint = semantic_acceptance_fingerprint(task, diff_text=diff_text)
                    previous = _prior_rejection(task, fingerprint)
                    if previous:
                        agent._append_event(
                            task_id,
                            {
                                "type": "semantic_acceptance_repeat_blocked",
                                "cycle": int(task.get("agent_cycle") or 0),
                                "fingerprint": fingerprint,
                                "summary": (
                                    "Skipped a duplicate semantic acceptance review because the "
                                    "acceptance-relevant state is unchanged."
                                ),
                            },
                        )
                        return _blocked_repeat_result(previous)
            except Exception:
                fingerprint = ""
                task = {}

        result = original_run(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )
        if (
            name == "coding_finish"
            and fingerprint
            and str(result.get("error") or "") == "semantic_acceptance_rejected"
        ):
            if not task:
                task = cw.load_task(task_id)
            _record_rejection(
                agent,
                task_id,
                task,
                fingerprint=fingerprint,
                result=result,
            )
        return result

    guarded._run_tool_with_semantic_acceptance = run_with_rejection_dedup
    # Preserve the public dispatch identity established by completion-state dispatch.
    agent._run_tool = run_with_rejection_dedup

    original_run_task_command = getattr(cw, "run_task_command", None)
    if callable(original_run_task_command):

        def run_task_command_with_validation_provenance(
            task_id: str,
            *args: Any,
            **kwargs: Any,
        ) -> Dict[str, Any]:
            result = original_run_task_command(task_id, *args, **kwargs)
            try:
                _persist_validation_provenance(
                    cw,
                    work_phases,
                    task_id=task_id,
                    argv=kwargs.get("argv"),
                    cwd=kwargs.get("cwd"),
                    result=result,
                )
            except Exception:
                # Provenance is controller bookkeeping; command execution semantics
                # must not change if persistence encounters legacy/corrupt state.
                pass
            return result

        cw.run_task_command = run_task_command_with_validation_provenance
        ensure_serialized = getattr(cw, "ensure_task_workspace_serialized", None)
        if callable(ensure_serialized):
            ensure_serialized("run_task_command")

    original_snapshot = cw.coding_state_snapshot

    def snapshot_with_validation_provenance(task_id: str) -> Dict[str, Any]:
        snapshot = original_snapshot(task_id)
        task = cw.load_task(task_id)
        output = dict(snapshot)
        validation = _reconciled_validation_state(
            task,
            snapshot,
            is_validation_command=work_phases.is_validation_command,
        )
        output["validation"] = validation
        output["progress"] = _reconciled_progress_state(snapshot, validation)
        return output

    cw.coding_state_snapshot = snapshot_with_validation_provenance
    guarded._terminal_acceptance_hardening_installed = True
