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
    call_id: str,
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
        "tool_call_id": call_id,
        "name": name,
        "args": args,
        "ts": ts,
    }


def _finished(name: str, *, cycle: int, ts: float, call_id: str):
    return {
        "type": "tool_finished",
        "cycle": cycle,
        "tool_call_id": call_id,
        "name": name,
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


def _ledger_update(existing, events, *, run_id, cycle, limit=32):
    ledger = {
        str(item.get("signature") or ""): dict(item)
        for item in existing
        if isinstance(item, dict) and str(item.get("signature") or "")
    }
    order = list(ledger)
    for event in events:
        if str(event.get("type") or "") != "tool_started":
            continue
        signature = _signature(event)
        if not signature:
            continue
        entry = ledger.get(signature, {"signature": signature, "count": 0})
        entry.update(
            {
                "target": _target(event),
                "count": int(entry.get("count") or 0) + 1,
                "last_run_id": run_id,
                "last_cycle": cycle,
                "last_seen_at": float(event.get("ts") or 0),
            }
        )
        ledger[signature] = entry
        if signature in order:
            order.remove(signature)
        order.append(signature)
    return [ledger[key] for key in order[-limit:]]


def _resilience(*, new_events_since):
    return SimpleNamespace(
        new_events_since=new_events_since,
        update_inspection_ledger=_ledger_update,
        inspection_signature=_signature,
        inspection_target=_target,
    )


def _install(new_events_since):
    resilience = _resilience(new_events_since=new_events_since)
    integrity.install(resilience)
    return resilience


def test_pending_start_is_not_persisted_before_tool_finishes():
    start = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="call-1",
    )
    resilience = _install(
        lambda events, controller, *, run_id, rollover_window=64: list(events)
    )

    new_events = resilience.new_events_since([start], {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [], new_events, run_id="run-1", cycle=9
    )

    assert result == []


def test_same_poll_success_is_persisted_after_completion():
    start = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="call-1",
    )
    finish = _finished(READ, cycle=9, ts=2, call_id="call-1")
    events = [start, finish]
    resilience = _install(
        lambda current, controller, *, run_id, rollover_window=64: list(current)
    )

    new_events = resilience.new_events_since(events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [], new_events, run_id="run-1", cycle=9
    )

    assert len(result) == 1
    assert result[0]["count"] == 1
    assert result[0]["target"] == _target(start)
    assert result[0][integrity._OCCURRENCE_KEYS_FIELD] == ["id:call-1"]


def test_same_poll_rejected_start_never_becomes_inspection_fact():
    start = _started(
        READ,
        cycle=9,
        path="services/gateway/app/static/image.js",
        ts=1,
        call_id="call-rejected",
    )
    rejection = _rejected(READ, cycle=9, ts=2)
    finish = _finished(READ, cycle=9, ts=3, call_id="call-rejected")
    events = [start, rejection, finish]
    resilience = _install(
        lambda current, controller, *, run_id, rollover_window=64: list(current)
    )

    new_events = resilience.new_events_since(events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [], new_events, run_id="run-1", cycle=9
    )

    assert result == []
    assert rejection in new_events


def test_split_poll_success_is_added_when_finish_arrives():
    start = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="call-split-success",
    )
    finish = _finished(READ, cycle=9, ts=2, call_id="call-split-success")
    full_events = [start, finish]
    resilience = _install(
        lambda events, controller, *, run_id, rollover_window=64: [events[-1]]
    )

    new_events = resilience.new_events_since(full_events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [], new_events, run_id="run-1", cycle=9
    )

    assert new_events == [finish]
    assert len(result) == 1
    assert result[0]["target"] == _target(start)
    assert result[0][integrity._OCCURRENCE_KEYS_FIELD] == [
        "id:call-split-success"
    ]


def test_cross_run_prior_metadata_survives_rejected_same_signature_attempt():
    prior = _started(
        READ,
        cycle=4,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="old-success",
        start_line=100,
    )
    existing = [
        {
            "signature": _signature(prior),
            "target": _target(prior),
            "count": 1,
            "last_run_id": "run-old",
            "last_cycle": 4,
            "last_seen_at": 1.0,
            integrity._OCCURRENCE_KEYS_FIELD: ["id:old-success"],
        }
    ]
    rejected = _started(
        READ,
        cycle=2,
        path="services/gateway/app/ui_routes.py",
        ts=10,
        call_id="new-rejected",
        start_line=200,
    )
    rejection = _rejected(READ, cycle=2, ts=11)
    finish = _finished(READ, cycle=2, ts=12, call_id="new-rejected")
    current_run_events = [rejected, rejection, finish]
    resilience = _install(
        lambda events, controller, *, run_id, rollover_window=64: list(events[1:])
    )

    new_events = resilience.new_events_since(
        current_run_events, {}, run_id="run-new"
    )
    result = resilience.update_inspection_ledger(
        existing, new_events, run_id="run-new", cycle=2
    )

    assert result == existing
    assert result[0]["last_run_id"] == "run-old"
    assert result[0]["last_cycle"] == 4
    assert result[0]["last_seen_at"] == 1.0
    assert result[0]["target"] == _target(prior)


def test_rollover_replay_rejection_does_not_decrement_unseen_start():
    prior = _started(
        READ,
        cycle=1,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="persisted-success",
        start_line=100,
    )
    existing = [
        {
            "signature": _signature(prior),
            "target": _target(prior),
            "count": 1,
            "last_run_id": "run-1",
            "last_cycle": 1,
            "last_seen_at": 1.0,
            integrity._OCCURRENCE_KEYS_FIELD: ["id:persisted-success"],
        }
    ]
    rejected = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=100,
        call_id="never-persisted",
        start_line=300,
    )
    filler = [
        {"type": "assistant", "cycle": 9, "ts": 101 + index, "content": "x"}
        for index in range(70)
    ]
    rejection = _rejected(READ, cycle=9, ts=200)
    finish = _finished(READ, cycle=9, ts=201, call_id="never-persisted")
    full_events = [rejected, *filler, rejection, finish]
    replay_tail = full_events[-64:]
    resilience = _install(
        lambda events, controller, *, run_id, rollover_window=64: replay_tail
    )

    new_events = resilience.new_events_since(full_events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        existing, new_events, run_id="run-1", cycle=9
    )

    assert rejected not in new_events
    assert rejection in new_events
    assert result == existing


def test_replayed_completed_success_is_idempotent_by_occurrence_key():
    start = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="call-once",
    )
    finish = _finished(READ, cycle=9, ts=2, call_id="call-once")
    full_events = [start, finish]
    resilience = _install(
        lambda events, controller, *, run_id, rollover_window=64: list(events)
    )

    first_new = resilience.new_events_since(full_events, {}, run_id="run-1")
    first = resilience.update_inspection_ledger(
        [], first_new, run_id="run-1", cycle=9
    )
    replay_new = resilience.new_events_since(full_events, {}, run_id="run-1")
    replayed = resilience.update_inspection_ledger(
        first, replay_new, run_id="run-1", cycle=9
    )

    assert first[0]["count"] == 1
    assert replayed[0]["count"] == 1
    assert replayed[0][integrity._OCCURRENCE_KEYS_FIELD] == ["id:call-once"]


def test_two_completed_successes_with_same_signature_count_separately():
    first = _started(
        READ,
        cycle=8,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="call-a",
        start_line=100,
    )
    second = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=3,
        call_id="call-b",
        start_line=200,
    )
    events = [
        first,
        _finished(READ, cycle=8, ts=2, call_id="call-a"),
        second,
        _finished(READ, cycle=9, ts=4, call_id="call-b"),
    ]
    resilience = _install(
        lambda current, controller, *, run_id, rollover_window=64: list(current)
    )

    new_events = resilience.new_events_since(events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [], new_events, run_id="run-1", cycle=9
    )

    assert len(result) == 1
    assert result[0]["count"] == 2
    assert result[0]["target"] == _target(second)
    assert result[0][integrity._OCCURRENCE_KEYS_FIELD] == [
        "id:call-a",
        "id:call-b",
    ]


def test_success_and_rejection_same_tool_cycle_are_distinguished_by_completion_order():
    successful = _started(
        READ,
        cycle=9,
        path="services/gateway/app/ui_routes.py",
        ts=1,
        call_id="call-good",
    )
    rejected = _started(
        READ,
        cycle=9,
        path="services/gateway/app/static/image.js",
        ts=3,
        call_id="call-bad",
    )
    rejection = _rejected(READ, cycle=9, ts=4)
    events = [
        successful,
        _finished(READ, cycle=9, ts=2, call_id="call-good"),
        rejected,
        rejection,
        _finished(READ, cycle=9, ts=5, call_id="call-bad"),
    ]
    resilience = _install(
        lambda current, controller, *, run_id, rollover_window=64: list(current)
    )

    new_events = resilience.new_events_since(events, {}, run_id="run-1")
    result = resilience.update_inspection_ledger(
        [], new_events, run_id="run-1", cycle=9
    )

    assert len(result) == 1
    assert result[0]["target"] == _target(successful)
    assert result[0]["count"] == 1
    assert rejection in new_events


def test_full_event_context_does_not_change_new_event_return_semantics():
    start = _started(
        READ,
        cycle=9,
        path="a.py",
        ts=1,
        call_id="call-1",
    )
    finish = _finished(READ, cycle=9, ts=2, call_id="call-1")
    events = [start, finish]
    expected = [finish]
    resilience = _install(
        lambda current, controller, *, run_id, rollover_window=64: expected
    )

    actual = resilience.new_events_since(events, {}, run_id="run-1")

    assert actual is expected
    assert actual == [finish]


def test_install_is_idempotent():
    resilience = _resilience(
        new_events_since=lambda *args, **kwargs: [],
    )
    integrity.install(resilience)
    installed_new = resilience.new_events_since
    installed_update = resilience.update_inspection_ledger

    integrity.install(resilience)

    assert resilience.new_events_since is installed_new
    assert resilience.update_inspection_ledger is installed_update
