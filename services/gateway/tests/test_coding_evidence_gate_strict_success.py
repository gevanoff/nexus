from __future__ import annotations

from app import coding_work_phases as phases


def test_source_evidence_requires_explicit_ok_true() -> None:
    events = [
        {
            "type": "tool_started",
            "tool_call_id": "read-1",
            "name": "coding_read_file",
            "args": {"path": "services/gateway/app/static/image.html"},
        },
        {
            "type": "tool_finished",
            "tool_call_id": "read-1",
            "name": "coding_read_file",
            "result": {"content": "ambiguous result without success marker"},
        },
        {
            "type": "tool_started",
            "tool_call_id": "search-1",
            "name": "coding_search_text",
            "args": {"path": "services/gateway/app/static", "query": "model_management"},
        },
        {
            "type": "tool_finished",
            "tool_call_id": "search-1",
            "name": "coding_search_text",
            "result": {"ok": "true", "matches": []},
        },
    ]

    assert phases.successful_source_evidence_targets(events) == set()


def test_source_evidence_accepts_only_boolean_true() -> None:
    events = [
        {
            "type": "tool_started",
            "tool_call_id": "read-1",
            "name": "coding_read_file",
            "args": {"path": "services/gateway/app/static/image.html"},
        },
        {
            "type": "tool_finished",
            "tool_call_id": "read-1",
            "name": "coding_read_file",
            "result": {"ok": True, "content": "confirmed"},
        },
    ]

    assert phases.successful_source_evidence_targets(events) == {
        "read:services/gateway/app/static/image.html"
    }


def test_equivalent_paths_do_not_mint_distinct_evidence_targets() -> None:
    events = []
    for call_id, path in (
        ("read-1", "./services//gateway/app/static/"),
        ("read-2", "services/gateway/app/static"),
    ):
        events.extend(
            [
                {
                    "type": "tool_started",
                    "tool_call_id": call_id,
                    "name": "coding_read_file",
                    "args": {"path": path},
                },
                {
                    "type": "tool_finished",
                    "tool_call_id": call_id,
                    "name": "coding_read_file",
                    "result": {"ok": True, "content": "confirmed"},
                },
            ]
        )

    assert phases.successful_source_evidence_targets(events) == {
        "read:services/gateway/app/static"
    }
