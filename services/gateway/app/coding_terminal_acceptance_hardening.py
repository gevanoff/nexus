from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping


_SCHEMA = "nexus_coding_semantic_acceptance_state.v1"
_REJECTION_EVENT = "semantic_acceptance_state"


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


def _reconciled_validation_state(
    task: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    is_validation_command: Any,
) -> Dict[str, Any]:
    changes = _mapping(snapshot.get("changes"))
    try:
        last_edit_at = float(changes.get("last_edit_at") or 0.0)
    except (TypeError, ValueError):
        last_edit_at = 0.0

    latest: Mapping[str, Any] = {}
    latest_ts = 0.0
    for raw in task.get("commands") or []:
        command = _mapping(raw)
        if str(command.get("label") or "") not in {"agent-command", "command"}:
            continue
        argv = command.get("argv")
        try:
            qualifies = bool(is_validation_command(argv))
        except Exception:
            qualifies = False
        if not qualifies:
            continue
        try:
            ts = float(command.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if not latest or ts >= latest_ts:
            latest = command
            latest_ts = ts

    if not latest:
        return {
            "last_validation_command": [],
            "last_validation_ok": None,
            "last_validation_at": 0.0,
            "validation_after_latest_edit": False,
        }

    return {
        "last_validation_command": [str(item) for item in (latest.get("argv") or [])],
        "last_validation_ok": bool(latest.get("ok")),
        "last_validation_at": latest_ts,
        "validation_after_latest_edit": bool(latest_ts and latest_ts >= last_edit_at),
    }


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

    original_snapshot = cw.coding_state_snapshot

    def snapshot_with_validation_provenance(task_id: str) -> Dict[str, Any]:
        snapshot = original_snapshot(task_id)
        task = cw.load_task(task_id)
        output = dict(snapshot)
        output["validation"] = _reconciled_validation_state(
            task,
            snapshot,
            is_validation_command=work_phases.is_validation_command,
        )
        return output

    cw.coding_state_snapshot = snapshot_with_validation_provenance
    guarded._terminal_acceptance_hardening_installed = True
