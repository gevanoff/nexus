from __future__ import annotations

import contextvars
from typing import Any, Mapping, Sequence


_FULL_EVENTS: contextvars.ContextVar[tuple[Mapping[str, Any], ...]] = contextvars.ContextVar(
    "nexus_coding_inspection_ledger_full_events",
    default=(),
)
_OCCURRENCE_KEYS_FIELD = "_nexus_completed_occurrences"
_OCCURRENCE_IDENTITIES_FIELD = "_nexus_completed_occurrence_identities"
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


def _event_is_in_batch(
    event: Mapping[str, Any],
    batch: Sequence[Mapping[str, Any]],
) -> bool:
    return any(candidate is event or candidate == event for candidate in batch)


def _occurrence_key(event: Mapping[str, Any]) -> str:
    """Return a durable identity for one Gateway-observed tool occurrence.

    Backend tool-call IDs are correlation hints, not unique identities: local
    models can reuse values such as ``call_1`` on later responses. Scope that
    untrusted value with Gateway-owned event coordinates so a later real call is
    not mistaken for replay of an earlier occurrence. The key intentionally
    excludes raw arguments, which can contain sensitive material.
    """
    name = str(event.get("name") or "").strip()
    cycle = _as_int(event.get("cycle"))
    ts = _as_float(event.get("ts"))
    call_id = str(event.get("tool_call_id") or "").strip()
    return f"event:{name}:{cycle}:{ts:.9f}:{call_id}"


def _display_occurrence_key(event: Mapping[str, Any]) -> str:
    """Return compact diagnostic correlation text; never use it for identity."""
    call_id = str(event.get("tool_call_id") or "").strip()
    if call_id:
        return f"id:{call_id}"
    return _occurrence_key(event)


def _matching_finish_index(
    full_events: Sequence[Mapping[str, Any]],
    start_index: int,
) -> int:
    start = full_events[start_index]
    call_id = str(start.get("tool_call_id") or "").strip()
    if not call_id:
        return -1
    for index in range(start_index + 1, len(full_events)):
        event = full_events[index]
        if str(event.get("type") or "") != "tool_finished":
            continue
        if str(event.get("tool_call_id") or "").strip() == call_id:
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

    Modern Coding Agent `tool_started` events always carry a `tool_call_id`.
    Those events are not durable inspection facts until their matching
    `tool_finished` exists and no forced rejection occurred in between.

    Historical/stored fixtures can predate tool-call IDs. Preserve their legacy
    immediate-ledger semantics so existing durable state and semantic-memory
    behavior remain backward compatible; the production rejection race is
    closed on the ID-bearing event stream emitted by the current agent.
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
        call_id = str(start.get("tool_call_id") or "").strip()
        if not call_id:
            if start_index in current_indexes:
                starts.append(start)
            continue
        finish_index = _matching_finish_index(full_events, start_index)
        if finish_index < 0:
            continue
        if start_index not in current_indexes and finish_index not in current_indexes:
            continue
        if _rejected_between(full_events, start_index, finish_index):
            continue
        starts.append(start)
    return starts


def _existing_occurrence_identities(existing: Any) -> set[str]:
    identities: set[str] = set()
    if not isinstance(existing, list):
        return identities
    for item in existing:
        if not isinstance(item, Mapping):
            continue
        raw = item.get(_OCCURRENCE_IDENTITIES_FIELD)
        if not isinstance(raw, list):
            continue
        identities.update(str(value).strip() for value in raw if str(value).strip())
    return identities


def _annotate_occurrences(
    resilience: Any,
    ledger: list[dict[str, Any]],
    starts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    display_by_signature: dict[str, list[str]] = {}
    identities_by_signature: dict[str, list[str]] = {}
    for start in starts:
        signature = str(resilience.inspection_signature(start) or "").strip()
        if signature:
            display_by_signature.setdefault(signature, []).append(
                _display_occurrence_key(start)
            )
            identities_by_signature.setdefault(signature, []).append(
                _occurrence_key(start)
            )

    output: list[dict[str, Any]] = []
    for raw in ledger:
        entry = dict(raw)
        signature = str(entry.get("signature") or "").strip()
        previous_display = entry.get(_OCCURRENCE_KEYS_FIELD)
        display_keys = (
            [str(value).strip() for value in previous_display if str(value).strip()]
            if isinstance(previous_display, list)
            else []
        )
        previous_identities = entry.get(_OCCURRENCE_IDENTITIES_FIELD)
        identities = (
            [
                str(value).strip()
                for value in previous_identities
                if str(value).strip()
            ]
            if isinstance(previous_identities, list)
            else []
        )
        for key in display_by_signature.get(signature, []):
            display_keys.append(key)
        for identity in identities_by_signature.get(signature, []):
            if identity not in identities:
                identities.append(identity)
        if display_keys:
            entry[_OCCURRENCE_KEYS_FIELD] = display_keys[-_MAX_OCCURRENCE_KEYS:]
        if identities:
            entry[_OCCURRENCE_IDENTITIES_FIELD] = identities[-_MAX_OCCURRENCE_KEYS:]
        output.append(entry)
    return output


def install(resilience: Any) -> None:
    """Persist modern inspection facts only after execution actually completes.

    Rejected attempts remain in the full event stream, so stagnation and
    noncompliance logic still observe them. ID-bearing inspection starts are
    committed only after a successful completion; Gateway-scoped occurrence
    identities make rollover replay idempotent even if a backend reuses call
    IDs. Compact backend IDs are retained only as diagnostic correlation text.
    ID-less legacy events retain prior behavior.
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
            seen = _existing_occurrence_identities(existing)
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
