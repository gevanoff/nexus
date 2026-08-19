from __future__ import annotations

from typing import Any, Mapping, Sequence


def _rejected_started_indexes(events: Sequence[Mapping[str, Any]]) -> set[int]:
    """Pair forced-action rejection events with the exact preceding tool start.

    The Coding Agent emits `tool_started`, then `forced_action_tool_rejected`,
    then `tool_finished` for a policy-disabled call. Pairing by event order keeps
    this precise even when multiple calls with the same tool name occur in one
    cycle. Older events do not persist a tool-call id on the rejection event, so
    adjacency/order is the durable discriminator available across deployments.
    """
    matched: set[int] = set()
    for index, event in enumerate(events):
        if str(event.get("type") or "") != "forced_action_tool_rejected":
            continue
        name = str(event.get("name") or "").strip()
        cycle = int(event.get("cycle") or 0)
        if not name:
            continue
        for candidate_index in range(index - 1, -1, -1):
            candidate = events[candidate_index]
            candidate_cycle = int(candidate.get("cycle") or 0)
            if cycle and candidate_cycle and candidate_cycle != cycle:
                break
            if candidate_index in matched:
                continue
            if (
                str(candidate.get("type") or "") == "tool_started"
                and str(candidate.get("name") or "").strip() == name
            ):
                matched.add(candidate_index)
                break
    return matched


def filter_rejected_inspection_attempts(
    events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    rejected = _rejected_started_indexes(events)
    if not rejected:
        return list(events)
    return [event for index, event in enumerate(events) if index not in rejected]


def install(resilience: Any) -> None:
    """Keep policy-rejected tool attempts out of durable inspection facts.

    Rejected attempts remain in the event stream, so stagnation/noncompliance
    classification can still observe them. Only the input to the inspection
    ledger is filtered: a tool that Nexus explicitly refused to execute must not
    later appear in controller guidance as an already-inspected repository path.
    """
    if bool(getattr(resilience, "_coding_inspection_ledger_integrity_installed", False)):
        return

    original_update = resilience.update_inspection_ledger

    def update_without_rejected_attempts(
        existing: Any,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        cycle: int,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        return original_update(
            existing,
            filter_rejected_inspection_attempts(events),
            run_id=run_id,
            cycle=cycle,
            limit=limit,
        )

    resilience.update_inspection_ledger = update_without_rejected_attempts
    resilience._coding_inspection_ledger_integrity_installed = True
