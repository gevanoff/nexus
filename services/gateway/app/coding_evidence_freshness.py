from __future__ import annotations

from typing import Any, Dict, Mapping


def _normalized_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def _event_timestamp(event: Mapping[str, Any]) -> float:
    try:
        return float(event.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _plan_updated_at(task: Mapping[str, Any]) -> float:
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), Mapping) else {}
    try:
        return float(plan.get("updated_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _events(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in (task.get("agent_events") or [])
        if isinstance(item, Mapping)
    ]


def _successful_tool_result(event: Mapping[str, Any], *, read: bool = False) -> bool:
    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    if str(result.get("error") or "").strip():
        return False
    if result.get("ok") is False:
        return False
    if read:
        return bool(_normalized_path(result.get("path"))) and "content" in result
    return result.get("ok") is True


def _latest_plan_update_index(events: list[Mapping[str, Any]]) -> int:
    latest = -1
    for index, event in enumerate(events):
        if (
            str(event.get("type") or "") == "tool_finished"
            and str(event.get("name") or "") == "coding_update_plan"
            and _successful_tool_result(event)
        ):
            latest = index
    return latest


def _latest_linked_read(
    events: list[Mapping[str, Any]],
    linked_targets: set[str],
) -> tuple[int, float]:
    latest_index = -1
    latest_ts = 0.0
    if not linked_targets:
        return latest_index, latest_ts
    for index, event in enumerate(events):
        if (
            str(event.get("type") or "") != "tool_finished"
            or str(event.get("name") or "") != "coding_read_file_lines"
            or not _successful_tool_result(event, read=True)
        ):
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        path = _normalized_path(result.get("path"))
        if path in linked_targets:
            latest_index = index
            latest_ts = _event_timestamp(event)
    return latest_index, latest_ts


def _hypothesis_is_stale(
    task: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    *,
    latest_read_index: int,
    latest_read_ts: float,
) -> tuple[bool, int, float, str]:
    """Return whether linked evidence demonstrably postdates the current plan.

    Event order is authoritative when both tool events survive in the bounded
    event ring. If the plan-update event has been compacted away, the durable
    project-plan ``updated_at`` timestamp preserves ordering. Legacy fixtures or
    old workspaces with neither marker are left unchanged rather than inventing
    staleness and deadlocking a previously qualified edit state.
    """
    latest_plan_index = _latest_plan_update_index(events)
    plan_updated_at = _plan_updated_at(task)

    if latest_plan_index >= 0:
        return (
            latest_plan_index < latest_read_index,
            latest_plan_index,
            plan_updated_at,
            "event_order",
        )
    if plan_updated_at > 0 and latest_read_ts > 0:
        return (
            latest_read_ts > plan_updated_at,
            latest_plan_index,
            plan_updated_at,
            "plan_timestamp",
        )
    return False, latest_plan_index, plan_updated_at, "legacy_unknown"


def refine_state(task: Mapping[str, Any], state: Mapping[str, Any]) -> Dict[str, Any]:
    """Require the structured hypothesis to postdate all linked causal reads.

    A corrective evidence read may validate or falsify the hypothesis that
    requested it. Merely making that path verified must never unlock editing on
    the unchanged hypothesis. The intended transition is:

        read linked evidence -> update hypothesis -> edit

    Event order is used when available, with the durable project-plan timestamp
    as the compaction-safe fallback.
    """
    out = dict(state)
    if str(out.get("action_kind") or "") != "edit":
        return out

    linked_targets = {
        _normalized_path(item)
        for item in (out.get("hypothesis_causal_targets") or [])
        if _normalized_path(item)
    }
    if not linked_targets:
        return out

    events = _events(task)
    latest_read_index, latest_read_ts = _latest_linked_read(events, linked_targets)
    if latest_read_index < 0:
        return out

    stale, latest_plan_index, plan_updated_at, source = _hypothesis_is_stale(
        task,
        events,
        latest_read_index=latest_read_index,
        latest_read_ts=latest_read_ts,
    )
    if not stale:
        return out

    out["action_kind"] = "evidence"
    out["allowed_tools"] = ["coding_finish", "coding_update_plan"]
    out["hypothesis_evidence_postdates_plan"] = True
    out["hypothesis_freshness_source"] = source
    out["latest_linked_evidence_event_index"] = latest_read_index
    out["latest_linked_evidence_at"] = latest_read_ts
    out["latest_hypothesis_plan_event_index"] = latest_plan_index
    out["latest_hypothesis_plan_updated_at"] = plan_updated_at
    out["required_action"] = (
        "Verified causal evidence was gathered after the current structured hypothesis. "
        "Revise the remediation hypothesis with coding_update_plan so it explicitly accounts "
        "for the latest evidence before editing. Repository evidence must continue to cite a "
        "verified full repository-relative causal target."
    )
    return out


def install(evidence_policy: Any) -> None:
    """Install the final evidence-freshness refinement after other policy overlays."""
    if bool(getattr(evidence_policy, "_coding_evidence_freshness_installed", False)):
        return

    original_apply = evidence_policy.apply_provenance_gate

    def apply_with_freshness(
        forced_action: Any,
        task: Mapping[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        effective = original_apply(forced_action, task, state)
        return refine_state(task, effective)

    evidence_policy.apply_provenance_gate = apply_with_freshness

    original_prompt = evidence_policy._provenance_prompt_context

    def prompt_with_freshness(base: Any, state: Mapping[str, Any]) -> str:
        if not state.get("hypothesis_evidence_postdates_plan"):
            return original_prompt(base, state)
        verified = ", ".join(state.get("causal_evidence_targets") or [])
        fields = "\n".join(
            f"{label}: <specific finding>" for label in base._HYPOTHESIS_FIELDS
        )
        allowed = ", ".join(state.get("allowed_tools") or [])
        return (
            "Controller forced-action mode is ACTIVE. The current hypothesis is stale because "
            "verified causal evidence was read after the most recent coding_update_plan. Do not "
            "edit or inspect further. Revise the structured hypothesis now so its claims account "
            "for that newer evidence. Verified causal targets: "
            f"{verified or '(none)'}. Use these exact labelled fields:\n{fields}\n"
            f"Available tools: {allowed or 'coding_finish'}."
        )

    evidence_policy._provenance_prompt_context = prompt_with_freshness
    evidence_policy._coding_evidence_freshness_installed = True
