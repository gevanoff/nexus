from __future__ import annotations

import contextvars
from typing import Any, Mapping, Sequence


_FULL_EVENTS: contextvars.ContextVar[tuple[Mapping[str, Any], ...]] = contextvars.ContextVar(
    "nexus_coding_inspection_ledger_full_events",
    default=(),
)
_OCCURRENCE_KEYS_FIELD = "_nexus_completed_occurrences"
_MAX_OCCURRENCE_KEYS = 128


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_key(event: Mapping[str, Any]) -> tuple[str, str, int, float, str]:
    return (
        str(event.get("type") or ""),
        str(event.get("name") or "").strip(),
        _as_int(event.get("cycle")),
        _as_float(event.get("ts")),
        str(event.get("tool_call_id") or "").strip(),
    )


def _find_event_index(
    full_events: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
) -> int:
    for index in range(len(full_events) - 1, -1, -1):
        if full_events[index] is event:
            return index
    key = _event_key(event)
    for index in range(len(full_events) - 1, -1, -1):
        if _event_key(full_events[index]) == key and full_events[index] == event:
            return index
    return -1


def _event_is_in_batch(
    event: Mapping[str, Any],
    batch: Sequence[Mapping[str, Any]],
) -> bool:
    return any(candidate is event or candidate == event for candidate in batch)


def _occurrence_key(event: Mapping[str, Any]) -> str:
    call_id = str(event.get("tool_call_id") or "").strip()
    if call_id:
        return f"id:{call_id}"
    name = str(event.get("name") or "").strip()
    cycle = _as_int(event.get("cycle"))
    ts = _as_float(event.get("ts"))
    return f"event:{name}:{cycle}:{ts:.9f}"


def _matching_finish_index(
    full_events: Sequence[Mapping[str, Any]],
    start_index: int,
) -> int:
    start = full_events[start_index]
    call_id = str(start.get("tool_call_id") or "").strip()
    name = str(start.get("name") or "").strip()
    cycle = _as_int(start.get("cycle"))
    for index in range(start_index + 1, len(full_events)):
        event = full_events[index]
        event_type = str(event.get("type") or "")
        if event_type != "tool_finished":
            if not call_id:
                event_cycle = _as_int(event.get("cycle"))
                if cycle and event_cycle and event_cycle != cycle:
                    break
            continue
        if call_id:
            if str(event.get("tool_call_id") or "").strip() == call_id:
                return index
            continue
        if (
            str(event.get("name") or "").strip() == name
            and _as_int(event.get("cycle")) == cycle
        ):
            return index
    return -1


def _rejected_between(
    full_events: Sequence[Mapping[str, Any]],
    start_index: int,
    finish_index: int,
) -> bool:
    start = full_events[start_index]
    name = str(start.get("name") or "").strip()
    cycle = _as_int(start.get("cycle"))
    for event in full_events[start_index + 1 : finish_index]:
        if str(event.get("type") or "") != "forced_action_tool_rejected":
            continue
        if (
            str(event.get("name") or "").strip() == name
            and _as_int(event.get("cycle")) == cycle
        ):
            return True
    return False


def _completed_valid_starts(
    resilience: Any,
    new_events: Sequence[Mapping[str, Any]],
    full_events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return newly observable successful ledger-relevant tool occurrences.

    A `tool_started` event is never a durable inspection fact by itself. We wait
    until its matching `tool_finished` is present and confirm that no forced
    rejection occurred between start and finish. This removes the need to undo a
    speculative ledger write later, so continuation-run metadata and rollover
    replay cannot be corrupted by a rejected attempt that was never executed.
    """
    current_indexes = {
        index
        for index, event in enumerate(full_events)
        if _event_is_in_batch(event, new_events)
    }
    starts: list[Mapping[str, Any]] = []
    for start_index, start in enumerate(full_events):
        if str(start.get("type") or "") != "tool_started":
            continue
        signature = str(resilience.inspection_signature(start) or "").strip()
        if not signature:
            continue
        finish_index = _matching_finish_index(full_events, start_index)
        if finish_index < 0:
            continue
        # Emit this occurrence only when either the start or its successful
        # completion became newly visible in this semantic-memory sample.
        if start_index not in current_indexes and finish_index not in current_indexes:
            continue
        if _rejected_between(full_events, start_index, finish_index):
            continue
        starts.append(start)
    return starts


def _existing_occurrence_keys(existing: Any) -> set[str]:
    keys: set[str] = set()
    if not isinstance(existing, list):
        return keys
    for item in existing:
        if not isinstance(item, Mapping):
            continue
        raw = item.get(_OCCURRENCE_KEYS_FIELD)
        if not isinstance(raw, list):
            continue
        keys.update(str(value).strip() for value in raw if str(value).strip())
    return keys


def _annotate_occurrences(
    resilience: Any,
    ledger: list[dict[str, Any]],
    starts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_signature: dict[str, list[str]] = {}
    for start in starts:
        signature = str(resilience.inspection_signature(start) or "").strip()
        if signature:
            by_signature.setdefault(signature, []).append(_occurrence_key(start))

    output: list[dict[str, Any]] = []
    for raw in ledger:
        entry = dict(raw)
        signature = str(entry.get("signature") or "").strip()
        previous = entry.get(_OCCURRENCE_KEYS_FIELD)
        keys = (
            [str(value).strip() for value in previous if str(value).strip()]
            if isinstance(previous, list)
            else []
        )
        for key in by_signature.get(signature, []):
            if key not in keys:
                keys.append(key)
        if keys:
            entry[_OCCURRENCE_KEYS_FIELD] = keys[-_MAX_OCCURRENCE_KEYS:]
        output.append(entry)
    return output


def install(resilience: Any) -> None:
    """Persist inspection facts only after tool execution actually completes.

    Rejected attempts remain in the full event stream, so stagnation and
    noncompliance logic still observe them. The inspection ledger receives only
    completed, non-rejected, ledger-relevant starts. Opaque occurrence keys make
    replay idempotent without treating a missing cursor as evidence that a start
    was previously persisted.
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

    def update_with_completed_occurrences(
        existing: Any,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        cycle: int,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        full_events = list(_FULL_EVENTS.get()) or list(events)
        try:
            completed = _completed_valid_starts(
                resilience,
                events,
                full_events,
            )
            seen = _existing_occurrence_keys(existing)
            fresh = [
                start
                for start in completed
                if _occurrence_key(start) not in seen
            ]
            ledger = original_update(
                existing,
                fresh,
                run_id=run_id,
                cycle=cycle,
                limit=limit,
            )
            return _annotate_occurrences(resilience, ledger, fresh)
        finally:
            _FULL_EVENTS.set(())

    resilience.new_events_since = new_events_with_full_context
    resilience.update_inspection_ledger = update_with_completed_occurrences
    resilience._coding_inspection_ledger_integrity_installed = True
