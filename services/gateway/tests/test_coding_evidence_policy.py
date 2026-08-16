from __future__ import annotations

from app import coding_evidence_policy as provenance
from app import coding_forced_action as forced


def _state() -> dict:
    return {
        "requires_hypothesis": True,
        "action_kind": "edit",
        "activation_plan_revision": 0,
        "activation_event_count": 0,
        "activated_at": 1.0,
    }


def _plan(repository_evidence: str) -> dict:
    return {
        "revision": 1,
        "goal": "Fix the Image UI",
        "note": (
            "Root cause: the backend navigation is missing from the rendering path.\n"
            f"Repository evidence: {repository_evidence}\n"
            "Competing explanation checked: configuration-only failure was checked.\n"
            "Expected result: the configured backend URL is visibly rendered."
        ),
        "items": [],
    }


def _search_events(path: str, *, call_id: str = "search-1") -> list[dict]:
    return [
        {
            "ts": 2.0,
            "type": "tool_started",
            "tool_call_id": call_id,
            "name": "coding_search_text",
            "args": {"path": path, "query": "InvokeAI"},
        },
        {
            "ts": 3.0,
            "type": "tool_finished",
            "tool_call_id": call_id,
            "name": "coding_search_text",
            "result": {"ok": True, "matches": []},
        },
    ]


def _read_events(path: str, *, call_id: str = "read-1") -> list[dict]:
    return [
        {
            "ts": 2.0,
            "type": "tool_started",
            "tool_call_id": call_id,
            "name": "coding_read_file_lines",
            "args": {"path": path, "start_line": 1, "line_count": 40},
        },
        {
            "ts": 3.0,
            "type": "tool_finished",
            "tool_call_id": call_id,
            "name": "coding_read_file_lines",
            "result": {"path": path, "content": "implementation"},
        },
    ]


def test_negative_test_fixture_is_acceptance_evidence_not_root_cause_evidence():
    path = "services/gateway/tests/test_coding_historical_qualification.py"
    task = {
        "agent_events": _search_events(path),
        "project_plan": _plan(path),
    }

    state = provenance.apply_provenance_gate(forced, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["causal_evidence_count"] == 0
    assert state["acceptance_evidence_targets"] == [path]
    assert state["hypothesis_causal_evidence_linked"] is False
    assert "coding_search_text" in state["allowed_tools"]
    assert "coding_read_file_lines" in state["allowed_tools"]
    assert "coding_apply_patch" not in state["allowed_tools"]


def test_causal_implementation_evidence_must_be_explicitly_linked_in_hypothesis():
    causal = "services/gateway/app/static/image_catalog_ui.js"
    fixture = "services/gateway/tests/test_coding_historical_qualification.py"
    task = {
        "agent_events": _read_events(causal),
        "project_plan": _plan(fixture),
    }

    state = provenance.apply_provenance_gate(forced, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["causal_evidence_targets"] == [causal]
    assert state["hypothesis_causal_evidence_linked"] is False
    assert state["allowed_tools"] == ["coding_finish", "coding_update_plan"]


def test_linked_causal_implementation_evidence_preserves_edit_unlock():
    causal = "services/gateway/app/static/image_catalog_ui.js"
    task = {
        "agent_events": _read_events(causal),
        "project_plan": _plan(
            "services/gateway/app/static/image_catalog_ui.js renders management.ui_url "
            "through safeExternalUrl."
        ),
    }

    state = provenance.apply_provenance_gate(forced, task, _state())

    assert state["action_kind"] == "edit"
    assert state["causal_evidence_targets"] == [causal]
    assert state["hypothesis_causal_targets"] == [causal]
    assert state["hypothesis_causal_evidence_linked"] is True
