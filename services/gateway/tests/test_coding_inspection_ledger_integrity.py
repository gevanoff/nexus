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


def test_rejected_read_start_is_removed_from_ledger_input():
    events = [
        _started(READ, cycle=9, path="services/gateway/app/static/image.js", ts=1),
        _rejected(READ, cycle=9, ts=2),
        {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 3},
    ]

    filtered = integrity.filter_rejected_inspection_attempts(events)

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

    filtered = integrity.filter_rejected_inspection_attempts(events)
    starts = [event for event in filtered if event.get("type") == "tool_started"]

    assert starts == [successful]


def test_rejection_does_not_pair_with_prior_cycle_start():
    prior = _started(
        READ,
        cycle=8,
        path="services/gateway/app/ui_routes.py",
        ts=1,
    )
    events = [prior, _rejected(READ, cycle=9, ts=2)]

    filtered = integrity.filter_rejected_inspection_attempts(events)

    assert prior in filtered


def test_different_tool_rejection_does_not_remove_valid_start():
    read = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
    )
    events = [read, _rejected(SEARCH, cycle=9, ts=2)]

    filtered = integrity.filter_rejected_inspection_attempts(events)

    assert read in filtered


def test_install_filters_only_ledger_input_and_preserves_rejection_events():
    seen = {}

    def update(existing, events, *, run_id, cycle, limit=32):
        seen["events"] = list(events)
        seen["run_id"] = run_id
        seen["cycle"] = cycle
        seen["limit"] = limit
        return [{"target": "ok"}]

    resilience = SimpleNamespace(update_inspection_ledger=update)
    integrity.install(resilience)
    events = [
        _started(READ, cycle=9, path="services/gateway/app/static/image.js", ts=1),
        _rejected(READ, cycle=9, ts=2),
        {"type": "tool_finished", "cycle": 9, "name": READ, "ts": 3},
    ]

    result = resilience.update_inspection_ledger(
        [],
        events,
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


def test_install_is_idempotent():
    resilience = SimpleNamespace(
        update_inspection_ledger=lambda *args, **kwargs: [],
    )
    integrity.install(resilience)
    installed = resilience.update_inspection_ledger

    integrity.install(resilience)

    assert resilience.update_inspection_ledger is installed
