from __future__ import annotations

import contextvars
from typing import Any, Mapping, Sequence


_FULL_EVENTS: contextvars.ContextVar[tuple[Mapping[str, Any], ...]] = contextvars.ContextVar(
    "nexus_coding_inspection_ledger_full_events",
    default=(),
)


def _event_key(event: Mapping[str, Any]) -> tuple[str, str, int, float]:
    try:
        cycle = int(event.get("cycle") or 0)
    except (TypeError, ValueError):
        cycle = 0
    try:
        ts = float(event.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return (
        str(event.get("type") or ""),
        str(event.get("name") or "").strip(),
        cycle,
        ts,
    )


def _find_event_index(
    full_events: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
) -> int:
    for index in range(len(full_events) - 1, -1, -1):
        candidate = full_events[index]
        if candidate is event:
            return index
    key = _event_key(event)
    for index in range(len(full_events) - 1, -1, -1):
        if _event_key(full_events[index]) == key:
            return index
    return -1


def _matching_rejected_starts(
    new_events: Sequence[Mapping[str, Any]],
    full_events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return the exact starts paired with newly observed forced rejections.

    `new_events` can begin after the matching `tool_started` when semantic memory
    polls between start and rejection. Search the complete durable event window
    for each newly observed rejection, then choose the nearest preceding
    same-name start in the same cycle. This stays precise when multiple calls to
    the same tool occur in one cycle.
    """
    matched_full_indexes: set[int] = set()
    starts: list[Mapping[str, Any]] = []
    for rejection in new_events:
        if str(rejection.get("type") or "") != "forced_action_tool_rejected":
            continue
        name = str(rejection.get("name") or "").strip()
        if not name:
            continue
        try:
            cycle = int(rejection.get("cycle") or 0)
        except (TypeError, ValueError):
            cycle = 0
        rejection_index = _find_event_index(full_events, rejection)
        if rejection_index < 0:
            continue
        for candidate_index in range(rejection_index - 1, -1, -1):
            candidate = full_events[candidate_index]
            try:
                candidate_cycle = int(candidate.get("cycle") or 0)
            except (TypeError, ValueError):
                candidate_cycle = 0
            if cycle and candidate_cycle and candidate_cycle != cycle:
                break
            if candidate_index in matched_full_indexes:
                continue
            if (
                str(candidate.get("type") or "") == "tool_started"
                and str(candidate.get("name") or "").strip() == name
            ):
                matched_full_indexes.add(candidate_index)
                starts.append(candidate)
                break
    return starts


def filter_rejected_inspection_attempts(
    events: Sequence[Mapping[str, Any]],
    *,
    full_events: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    full = list(full_events or events)
    starts = _matching_rejected_starts(events, full)
    rejected_ids = {id(event) for event in starts}
    if not rejected_ids:
        return list(events), []
    filtered = [event for event in events if id(event) not in rejected_ids]
    return filtered, starts


def _remove_existing_signatures(
    resilience: Any,
    existing: Any,
    rejected_starts: Sequence[Mapping[str, Any]],
) -> Any:
    if not isinstance(existing, list) or not rejected_starts:
        return existing
    rejected_signatures = {
        str(resilience.inspection_signature(event) or "").strip()
        for event in rejected_starts
    }
    rejected_signatures.discard("")
    if not rejected_signatures:
        return existing
    return [
        item
        for item in existing
        if not (
            isinstance(item, Mapping)
            and str(item.get("signature") or "").strip() in rejected_signatures
        )
    ]


def install(resilience: Any) -> None:
    """Keep policy-rejected tool attempts out of durable inspection facts.

    Rejected attempts remain in the event stream, so stagnation/noncompliance
    classification can still observe them. Only the inspection ledger is
    reconciled: a tool that Nexus explicitly refused to execute must not later
    appear in controller guidance as an already-inspected repository path.

    The full event window is captured alongside `new_events_since` without
    changing its return value, cursor accounting, or processed-event totals.
    This lets a later rejection remove a false ledger fact even when semantic
    memory polled after `tool_started` but before the rejection was emitted.
    """
    if bool(getattr(resilience, "_coding_inspection_ledger_integrity_installed", False)):
        return

    original_new_events = resilience.new_events_since
    original_update = resilience.update_inspection_ledger

    def new_events_with_full_context(
        events: Sequence[Mapping[str, Any]],
        controller: Mapping[str, Any],
        *,
        run_id: str,
        rollover_window: int = 64,
    ) -> list[Mapping[str, Any]]:
        _FULL_EVENTS.set(tuple(events))
        return original_new_events(
            events,
            controller,
            run_id=run_id,
            rollover_window=rollover_window,
        )

    def update_without_rejected_attempts(
        existing: Any,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        cycle: int,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        full_events = list(_FULL_EVENTS.get()) or list(events)
        try:
            filtered, rejected_starts = filter_rejected_inspection_attempts(
                events,
                full_events=full_events,
            )
            reconciled_existing = _remove_existing_signatures(
                resilience,
                existing,
                rejected_starts,
            )
            return original_update(
                reconciled_existing,
                filtered,
                run_id=run_id,
                cycle=cycle,
                limit=limit,
            )
        finally:
            _FULL_EVENTS.set(())

    resilience.new_events_since = new_events_with_full_context
    resilience.update_inspection_ledger = update_without_rejected_attempts
    resilience._coding_inspection_ledger_integrity_installed = True
