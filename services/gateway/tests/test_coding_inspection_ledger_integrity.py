from __future__ import annotations

from types import SimpleNamespace

from app import coding_inspection_ledger_integrity as integrity


READ = "coding_read_file_lines"
SEARCH = "coding_search_text"


def _started(
    name: str,
    *,
    cycle: int,
    path: str,
    ts: float,
    start_line: int = 10,
):
    args = {"path": path}
    if name == READ:
        args.update({"start_line": start_line, "line_count": 20})
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


def _target(event):
    args = event.get("args") or {}
    path = str(args.get("path") or "")
    if event.get("name") == READ:
        start = int(args.get("start_line") or 0)
        count = int(args.get("line_count") or 0)
        return f"read {path} lines {start}-{start + count - 1}"
    return f"search {path}: {args.get('query') or ''}".rstrip()


def _resilience(*, new_events_since, update_inspection_ledger):
    return SimpleNamespace(
        new_events_since=new_events_since,
        update_inspection_ledger=update_inspection_ledger,
        inspection_signature=_signature,
        inspection_target=_target,
    )


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
        return list(events[1:])

    def update(existing, events, *, run_id, cycle, limit=32):
        seen["existing"] = list(existing)
        seen["events"] = list(events)
        return list(existing)

    resilience = _resilience(
        new_events_since=new_events_since,
        update_inspection_ledger=update,
    )
    integrity.install(resilience)
    existing = [
        {
            "signature": _signature(started),
            "target": _target(started),
            "count": 1,
            "last_run_id": "run-1",
            "last_cycle": 9,
            "last_seen_at": 1.0,
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


def test_split_poll_rejection_preserves_prior_valid_same_signature():
    seen = {}
    valid = _started(
        READ,
        cycle=8,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        start_line=100,
    )
    rejected = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=3,
        start_line=200,
    )
    rejection = _rejected(READ, cycle=9, ts=4)
    finished = {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 5}
    full_events = [
        valid,
        {"type": "tool_finished", "cycle": 8, "name": READ, "ts": 2},
        rejected,
        rejection,
        finished,
    ]

    def new_events_since(events, controller, *, run_id, rollover_window=64):
        # Prior poll already persisted both starts; this poll sees only the
        # rejection outcome for the second occurrence.
        return list(events[3:])

    def update(existing, events, *, run_id, cycle, limit=32):
        seen["existing"] = [dict(item) for item in existing]
        seen["events"] = list(events)
        return [dict(item) for item in existing]

    resilience = _resilience(
        new_events_since=new_events_since,
        update_inspection_ledger=update,
    )
    integrity.install(resilience)
    existing = [
        {
            "signature": _signature(rejected),
            "target": _target(rejected),
            "count": 2,
            "last_run_id": "run-1",
            "last_cycle": 9,
            "last_seen_at": 3.0,
        }
    ]

    new_events = resilience.new_events_since(full_events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        existing,
        new_events,
        run_id="run-1",
        cycle=9,
    )

    assert len(result) == 1
    restored = result[0]
    assert restored["count"] == 1
    assert restored["target"] == _target(valid)
    assert restored["last_cycle"] == 8
    assert restored["last_seen_at"] == 1.0
    assert seen["existing"] == [restored]
    assert seen["events"] == [rejection, finished]


def test_same_poll_rejected_same_signature_does_not_decrement_prior_existing_fact():
    seen = {}
    prior = _started(
        READ,
        cycle=8,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        start_line=100,
    )
    rejected = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=3,
        start_line=200,
    )
    rejection = _rejected(READ, cycle=9, ts=4)
    full_events = [prior, rejected, rejection]

    def new_events_since(events, controller, *, run_id, rollover_window=64):
        return list(events[1:])

    def update(existing, events, *, run_id, cycle, limit=32):
        seen["existing"] = [dict(item) for item in existing]
        seen["events"] = list(events)
        return [dict(item) for item in existing]

    resilience = _resilience(
        new_events_since=new_events_since,
        update_inspection_ledger=update,
    )
    integrity.install(resilience)
    existing = [
        {
            "signature": _signature(prior),
            "target": _target(prior),
            "count": 1,
            "last_run_id": "run-1",
            "last_cycle": 8,
            "last_seen_at": 1.0,
        }
    ]

    new_events = resilience.new_events_since(full_events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        existing,
        new_events,
        run_id="run-1",
        cycle=9,
    )

    assert result == existing
    assert seen["existing"] == existing
    assert seen["events"] == [rejection]


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

    resilience = _resilience(
        new_events_since=new_events_since,
        update_inspection_ledger=update,
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

    resilience = _resilience(
        new_events_since=(
            lambda current, controller, *, run_id, rollover_window=64: expected
        ),
        update_inspection_ledger=(lambda *args, **kwargs: []),
    )
    integrity.install(resilience)

    actual = resilience.new_events_since(events, {}, run_id="run-1")

    assert actual is expected
    assert len(actual) == 1


def test_install_is_idempotent():
    resilience = _resilience(
        new_events_since=lambda *args, **kwargs: [],
        update_inspection_ledger=lambda *args, **kwargs: [],
    )
    integrity.install(resilience)
    installed_new = resilience.new_events_since
    installed_update = resilience.update_inspection_ledger

    integrity.install(resilience)

    assert resilience.new_events_since is installed_new
    assert resilience.update_inspection_ledger is installed_update
