from __future__ import annotations

from types import SimpleNamespace

from app import coding_inspection_ledger_integrity as integrity


READ = "coding_read_file_lines"
SEARCH = "coding_search_text"


def _started(name: str, *, cycle: int, path: str, ts: float):
    args = {"path": path}
    if name == READ:
        args.update({"start_line": 10, "line_count": 20})
    else:
        args["query"] = "needle"
    return {
        "type": "tool_started",
        "cycle": cycle,
        "name": name,
        "args": args,
        "ts": ts,
    }


def _rejected(name: str, *, cycle: int, ts: float):
    return {
        "type": "forced_action_tool_rejected",
        "cycle": cycle,
        "name": name,
        "ts": ts,
    }


def _signature(event):
    path = str((event.get("args") or {}).get("path") or "")
    return f"{event.get('name')}:{path}" if path else ""


def test_rejected_read_start_is_removed_from_ledger_input():
    started = _started(
        READ,
        cycle=9,
        path="services/gateway/app/static/image.js",
        ts=1,
    )
    events = [
        started,
        _rejected(READ, cycle=9, ts=2),
        {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 3},
    ]

    filtered, rejected_starts = integrity.filter_rejected_inspection_attempts(events)

    assert rejected_starts == [started]
    assert not any(event.get("type") == "tool_started" for event in filtered)
    assert any(event.get("type") == "forced_action_tool_rejected" for event in filtered)
    assert any(event.get("type") == "tool_finished" for event in filtered)


def test_nearest_same_name_start_is_paired_when_one_call_succeeds_then_one_is_rejected():
    successful = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
    )
    rejected = _started(
        READ,
        cycle=9,
        path="services/gateway/app/static/image.js",
        ts=3,
    )
    events = [
        successful,
        {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 2},
        rejected,
        _rejected(READ, cycle=9, ts=4),
        {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 5},
    ]

    filtered, rejected_starts = integrity.filter_rejected_inspection_attempts(events)
    starts = [event for event in filtered if event.get("type") == "tool_started"]

    assert rejected_starts == [rejected]
    assert starts == [successful]


def test_rejection_does_not_pair_with_prior_cycle_start():
    prior = _started(
        READ,
        cycle=8,
        path="services/gateway/app/ui_routes.py",
        ts=1,
    )
    events = [prior, _rejected(READ, cycle=9, ts=2)]

    filtered, rejected_starts = integrity.filter_rejected_inspection_attempts(events)

    assert rejected_starts == []
    assert prior in filtered


def test_different_tool_rejection_does_not_remove_valid_start():
    read = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
    )
    events = [read, _rejected(SEARCH, cycle=9, ts=2)]

    filtered, rejected_starts = integrity.filter_rejected_inspection_attempts(events)

    assert rejected_starts == []
    assert read in filtered


def test_split_poll_rejection_removes_already_persisted_false_inspection_fact():
    seen = {}
    started = _started(
        READ,
        cycle=9,
        path="services/gateway/app/static/image.js",
        ts=1,
    )
    rejection = _rejected(READ, cycle=9, ts=2)
    finished = {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 3}
    full_events = [started, rejection, finished]

    def new_events_since(events, controller, *, run_id, rollover_window=64):
        # Simulate a prior semantic-memory poll whose cursor landed immediately
        # after tool_started. Only rejection + finish are new this time.
        return list(events[1:])

    def update(existing, events, *, run_id, cycle, limit=32):
        seen["existing"] = list(existing)
        seen["events"] = list(events)
        return list(existing)

    resilience = SimpleNamespace(
        new_events_since=new_events_since,
        update_inspection_ledger=update,
        inspection_signature=_signature,
    )
    integrity.install(resilience)
    existing = [
        {
            "signature": _signature(started),
            "target": "read services/gateway/app/static/image.js lines 10-29",
            "count": 1,
        }
    ]

    new_events = resilience.new_events_since(
        full_events,
        {"processed_event_cursor": "after-start"},
        run_id="run-1",
    )
    result = resilience.update_inspection_ledger(
        existing,
        new_events,
        run_id="run-1",
        cycle=9,
    )

    assert result == []
    assert seen["existing"] == []
    assert seen["events"] == [rejection, finished]


def test_same_poll_filter_preserves_rejection_for_noncompliance_observation():
    seen = {}

    def new_events_since(events, controller, *, run_id, rollover_window=64):
        return list(events)

    def update(existing, events, *, run_id, cycle, limit=32):
        seen["events"] = list(events)
        seen["run_id"] = run_id
        seen["cycle"] = cycle
        seen["limit"] = limit
        return [{"target": "ok"}]

    resilience = SimpleNamespace(
        new_events_since=new_events_since,
        update_inspection_ledger=update,
        inspection_signature=_signature,
    )
    integrity.install(resilience)
    events = [
        _started(READ, cycle=9, path="services/gateway/app/static/image.js", ts=1),
        _rejected(READ, cycle=9, ts=2),
        {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 3},
    ]

    new_events = resilience.new_events_since(events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [],
        new_events,
        run_id="run-1",
        cycle=9,
        limit=12,
    )

    assert result == [{"target": "ok"}]
    assert not any(event.get("type") == "tool_started" for event in seen["events"])
    assert any(
        event.get("type") == "forced_action_tool_rejected"
        for event in seen["events"]
    )
    assert seen["run_id"] == "run-1"
    assert seen["cycle"] == 9
    assert seen["limit"] == 12


def test_full_event_context_does_not_change_new_event_return_or_cursor_semantics():
    events = [
        _started(READ, cycle=9, path="a.py", ts=1),
        _rejected(READ, cycle=9, ts=2),
    ]
    expected = [events[1]]

    resilience = SimpleNamespace(
        new_events_since=(
            lambda current, controller, *, run_id, rollover_window=64: expected
        ),
        update_inspection_ledger=(lambda *args, **kwargs: []),
        inspection_signature=_signature,
    )
    integrity.install(resilience)

    actual = resilience.new_events_since(events, {}, run_id="run-1")

    assert actual is expected
    assert len(actual) == 1


def test_install_is_idempotent():
    resilience = SimpleNamespace(
        new_events_since=lambda *args, **kwargs: [],
        update_inspection_ledger=lambda *args, **kwargs: [],
        inspection_signature=_signature,
    )
    integrity.install(resilience)
    installed_new = resilience.new_events_since
    installed_update = resilience.update_inspection_ledger

    integrity.install(resilience)

    assert resilience.new_events_since is installed_new
    assert resilience.update_inspection_ledger is installed_update
