from __future__ import annotations

from types import SimpleNamespace

from app import coding_inspection_ledger_integrity as integrity


READ = "coding_read_file_lines"
PATH = "services/gateway/app/ui_routes.py"


def _start(*, start_line: int) -> dict:
    return {
        "type": "tool_started",
        "cycle": 9,
        "tool_call_id": "call_1",
        "name": READ,
        "args": {
            "path": PATH,
            "start_line": start_line,
            "line_count": 20,
        },
    }


def _finish() -> dict:
    return {
        "type": "tool_finished",
        "cycle": 9,
        "tool_call_id": "call_1",
        "name": READ,
    }


def _signature(event: dict) -> str:
    return f"read:{event['args']['path']}"


def _target(event: dict) -> str:
    args = event["args"]
    start = int(args["start_line"])
    return f"read {args['path']} lines {start}-{start + int(args['line_count']) - 1}"


def _ledger_update(existing, events, *, run_id, cycle, limit=32):
    ledger = {item["signature"]: dict(item) for item in existing}
    order = list(ledger)
    for event in events:
        signature = _signature(event)
        entry = ledger.get(signature, {"signature": signature, "count": 0})
        entry.update(
            {
                "target": _target(event),
                "count": int(entry.get("count") or 0) + 1,
                "last_run_id": run_id,
                "last_cycle": int(event.get("cycle") or cycle),
                "last_seen_at": float(event.get("ts") or 0),
            }
        )
        ledger[signature] = entry
        if signature in order:
            order.remove(signature)
        order.append(signature)
    return [ledger[key] for key in order[-limit:]]


def test_same_cycle_same_second_reused_backend_call_id_is_distinct_occurrence():
    persisted: list[dict] = []

    # Model the real _append_event boundary: the wrapper must provide uniqueness
    # before the original appender stamps its one-second-resolution timestamp.
    def append_event(_task_id: str, event: dict) -> dict:
        stored = {"ts": 100, **event}
        persisted.append(stored)
        return stored

    agent = SimpleNamespace(_append_event=append_event)
    resilience = SimpleNamespace(
        new_events_since=(
            lambda current, controller, *, run_id, rollover_window=64: list(current)
        ),
        update_inspection_ledger=_ledger_update,
        inspection_signature=_signature,
        inspection_target=_target,
    )
    integrity.install(resilience, agent)

    first = agent._append_event("task-1", _start(start_line=100))
    agent._append_event("task-1", _finish())
    second = agent._append_event("task-1", _start(start_line=200))
    agent._append_event("task-1", _finish())

    assert first["tool_call_id"] == second["tool_call_id"] == "call_1"
    assert first["cycle"] == second["cycle"] == 9
    assert first["ts"] == second["ts"] == 100
    assert first[integrity._EVENT_ID_FIELD] != second[integrity._EVENT_ID_FIELD]

    new_events = resilience.new_events_since(persisted, {}, run_id="run-1")
    ledger = resilience.update_inspection_ledger(
        [],
        new_events,
        run_id="run-1",
        cycle=9,
    )

    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["count"] == 2
    assert entry["target"] == _target(second)
    # The backend correlation text can repeat; Gateway replay identity cannot.
    assert entry[integrity._OCCURRENCE_KEYS_FIELD] == ["id:call_1", "id:call_1"]
    identities = entry[integrity._OCCURRENCE_IDENTITIES_FIELD]
    assert identities == [
        integrity._occurrence_key(first),
        integrity._occurrence_key(second),
    ]
    assert len(set(identities)) == 2
    assert all(value.startswith("event-id:") for value in identities)
