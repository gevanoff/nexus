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
        if _event_key(full_events[index]) == key and full_events[index] == event:
            return index
    return -1


def _matching_rejected_starts(
    rejection_events: Sequence[Mapping[str, Any]],
    full_events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return exact starts paired with the supplied forced-action rejections."""
    matched_full_indexes: set[int] = set()
    starts: list[Mapping[str, Any]] = []
    for rejection in rejection_events:
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


def _all_rejected_starts(
    full_events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    rejections = [
        event
        for event in full_events
        if str(event.get("type") or "") == "forced_action_tool_rejected"
    ]
    return _matching_rejected_starts(rejections, full_events)


def _event_is_in_batch(
    event: Mapping[str, Any],
    batch: Sequence[Mapping[str, Any]],
) -> bool:
    return any(candidate is event or candidate == event for candidate in batch)


def filter_rejected_inspection_attempts(
    events: Sequence[Mapping[str, Any]],
    *,
    full_events: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    full = list(full_events or events)
    starts = _matching_rejected_starts(events, full)
    rejected_ids = {
        id(event)
        for event in starts
        if _event_is_in_batch(event, events)
    }
    if not rejected_ids:
        return list(events), starts
    filtered = [event for event in events if id(event) not in rejected_ids]
    return filtered, starts


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _latest_prior_valid_start(
    resilience: Any,
    full_events: Sequence[Mapping[str, Any]],
    rejected_start: Mapping[str, Any],
    *,
    signature: str,
    all_rejected_ids: set[int],
) -> Mapping[str, Any] | None:
    rejected_index = _find_event_index(full_events, rejected_start)
    if rejected_index < 0:
        return None
    for index in range(rejected_index - 1, -1, -1):
        candidate = full_events[index]
        if id(candidate) in all_rejected_ids:
            continue
        if str(candidate.get("type") or "") != "tool_started":
            continue
        candidate_signature = str(
            resilience.inspection_signature(candidate) or ""
        ).strip()
        if candidate_signature == signature:
            return candidate
    return None


def _reconcile_existing_occurrences(
    resilience: Any,
    existing: Any,
    rejected_starts: Sequence[Mapping[str, Any]],
    *,
    current_events: Sequence[Mapping[str, Any]],
    full_events: Sequence[Mapping[str, Any]],
    run_id: str,
) -> Any:
    """Remove only rejected occurrences that a prior poll already persisted.

    A same-poll rejected start is filtered before the original ledger updater and
    therefore must not decrement existing state. A split-poll rejected start is
    already represented in `existing`; decrement exactly that occurrence. If a
    legitimate earlier occurrence of the same signature remains, restore the
    entry's latest target/timestamp metadata to that prior valid start rather
    than erasing the whole inspection fact.
    """
    if not isinstance(existing, list) or not rejected_starts:
        return existing

    split_rejected = [
        start
        for start in rejected_starts
        if not _event_is_in_batch(start, current_events)
    ]
    if not split_rejected:
        return existing

    all_rejected_ids = {id(event) for event in _all_rejected_starts(full_events)}
    by_signature: dict[str, list[Mapping[str, Any]]] = {}
    for start in split_rejected:
        signature = str(resilience.inspection_signature(start) or "").strip()
        if signature:
            by_signature.setdefault(signature, []).append(start)

    output: list[Any] = []
    for raw in existing:
        if not isinstance(raw, Mapping):
            output.append(raw)
            continue
        signature = str(raw.get("signature") or "").strip()
        rejected_for_signature = by_signature.get(signature)
        if not rejected_for_signature:
            output.append(dict(raw))
            continue

        entry = dict(raw)
        remaining_count = max(
            0,
            _as_int(entry.get("count")) - len(rejected_for_signature),
        )
        if remaining_count <= 0:
            continue

        latest_rejected = max(
            rejected_for_signature,
            key=lambda event: _find_event_index(full_events, event),
        )
        prior = _latest_prior_valid_start(
            resilience,
            full_events,
            latest_rejected,
            signature=signature,
            all_rejected_ids=all_rejected_ids,
        )
        entry["count"] = remaining_count
        if prior is not None:
            target = str(resilience.inspection_target(prior) or "").strip()
            if target:
                entry["target"] = target
            entry["last_run_id"] = run_id
            try:
                entry["last_cycle"] = int(prior.get("cycle") or 0)
            except (TypeError, ValueError):
                entry["last_cycle"] = 0
            try:
                entry["last_seen_at"] = float(prior.get("ts") or 0)
            except (TypeError, ValueError):
                entry["last_seen_at"] = 0.0
        output.append(entry)
    return output


def install(resilience: Any) -> None:
    """Keep policy-rejected tool attempts out of durable inspection facts.

    Rejected attempts remain in the event stream, so stagnation/noncompliance
    classification can still observe them. Only the inspection ledger is
    reconciled: a tool that Nexus explicitly refused to execute must not later
    appear in controller guidance as an already-inspected repository path.

    The full event window is captured alongside `new_events_since` without
    changing its return value, cursor accounting, or processed-event totals.
    This lets a later rejection repair a false ledger occurrence even when
    semantic memory polled after `tool_started` but before rejection was emitted.
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
            reconciled_existing = _reconcile_existing_occurrences(
                resilience,
                existing,
                rejected_starts,
                current_events=events,
                full_events=full_events,
                run_id=run_id,
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
