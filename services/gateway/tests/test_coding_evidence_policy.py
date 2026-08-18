from __future__ import annotations

from app import coding_evidence_policy as provenance
from app import coding_forced_action as forced


def _state(*, action_kind: str = "edit", activation_event_count: int = 0) -> dict:
    return {
        "requires_hypothesis": True,
        "action_kind": action_kind,
        "canonical_action_kind": "edit",
        "canonical_required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        "activation_plan_revision": 0,
        "activation_event_count": activation_event_count,
        "activated_at": 10.0,
        "run_id": "run-1",
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


def _started(*, run_id: str = "run-1", ts: float = 1.0) -> dict:
    return {
        "ts": ts,
        "type": "started",
        "run_id": run_id,
        "backend": "local_mlx",
    }


def _search_events(path: str, *, call_id: str = "search-1") -> list[dict]:
    return [
        {
            "ts": 2.0,
            "type": "tool_started",
            "tool_call_id": call_id,
            "name": "coding_search_text",
            "args": {"path": str(path.rsplit("/", 1)[0]), "query": "InvokeAI"},
        },
        {
            "ts": 3.0,
            "type": "tool_finished",
            "tool_call_id": call_id,
            "name": "coding_search_text",
            "result": {"ok": True, "matches": [f"{path}:42:InvokeAI"]},
        },
    ]


def _read_events(path: str, *, call_id: str = "read-1", ts: float = 4.0) -> list[dict]:
    return [
        {
            "ts": ts,
            "type": "tool_started",
            "tool_call_id": call_id,
            "name": "coding_read_file_lines",
            "args": {"path": path, "start_line": 1, "line_count": 40},
        },
        {
            "ts": ts + 1.0,
            "type": "tool_finished",
            "tool_call_id": call_id,
            "name": "coding_read_file_lines",
            "result": {"path": path, "content": "implementation"},
        },
    ]


def test_negative_test_fixture_is_acceptance_evidence_not_root_cause_evidence():
    path = "services/gateway/tests/test_coding_historical_qualification.py"
    task = {
        "agent_events": [_started(), *_search_events(path)],
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


def test_search_hit_is_candidate_not_verified_causal_evidence():
    causal = "services/gateway/app/static/image_catalog_ui.js"
    task = {
        "agent_events": [_started(), *_search_events(causal)],
        "project_plan": _plan(causal),
    }

    state = provenance.apply_provenance_gate(forced, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["candidate_causal_evidence_targets"] == [causal]
    assert state["causal_evidence_targets"] == []
    assert state["allowed_tools"] == ["coding_finish", "coding_read_file_lines"]
    assert state["hypothesis_causal_evidence_linked"] is False


def test_verified_implementation_requires_hypothesis_link_before_edit():
    causal = "services/gateway/app/static/image_catalog_ui.js"
    fixture = "services/gateway/tests/test_coding_historical_qualification.py"
    task = {
        "agent_events": [_started(), *_search_events(causal), *_read_events(causal)],
        "project_plan": _plan(fixture),
    }

    state = provenance.apply_provenance_gate(forced, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["causal_evidence_targets"] == [causal]
    assert state["hypothesis_causal_evidence_linked"] is False
    assert state["allowed_tools"] == ["coding_finish", "coding_update_plan"]


def test_linked_verified_evidence_promotes_to_edit_without_legacy_gate_race():
    causal = "services/gateway/app/static/image_catalog_ui.js"
    task = {
        "agent_events": [_started(), *_search_events(causal), *_read_events(causal)],
        "project_plan": _plan(
            "services/gateway/app/static/image_catalog_ui.js renders management.ui_url "
            "through safeExternalUrl."
        ),
    }

    # Reproduce the production deadlock: the persisted/base state may still say
    # evidence on the next turn. Provenance readiness must itself atomically
    # promote the execution policy and advertise edit tools.
    state = provenance.apply_provenance_gate(
        forced,
        task,
        _state(action_kind="evidence"),
    )

    assert state["action_kind"] == "edit"
    assert state["causal_evidence_targets"] == [causal]
    assert state["hypothesis_causal_targets"] == [causal]
    assert state["hypothesis_causal_evidence_linked"] is True
    assert "coding_apply_patch" in state["allowed_tools"]
    assert "coding_replace_text" in state["allowed_tools"]
    assert "coding_write_file" in state["allowed_tools"]
    assert "coding_search_text" not in state["allowed_tools"]


def test_preactivation_read_from_forced_action_run_remains_verified_after_resume():
    causal = "services/gateway/app/static/image.js"
    events = [_started(), *_read_events(causal)]
    task = {
        "agent_events": events,
        "project_plan": _plan(causal),
    }
    # The forced action activated after the read, as happened in the attached
    # production run. activation_event_count would previously discard the read.
    state = _state(action_kind="evidence", activation_event_count=len(events))

    effective = provenance.apply_provenance_gate(forced, task, state)

    assert effective["causal_evidence_targets"] == [causal]
    assert effective["action_kind"] == "edit"
    assert "coding_apply_patch" in effective["allowed_tools"]


def test_resumed_run_id_does_not_move_evidence_window_past_originating_run():
    causal = "services/gateway/app/static/image.js"
    first_run = [_started(run_id="run-1", ts=1.0), *_read_events(causal, ts=4.0)]
    events = [
        *first_run,
        _started(run_id="run-2", ts=20.0),
        {
            "ts": 21.0,
            "type": "cycle_started",
            "cycle": 1,
        },
    ]
    task = {
        "agent_events": events,
        "project_plan": _plan(causal),
    }
    state = _state(action_kind="evidence", activation_event_count=len(first_run))
    # coding_forced_action.activate() updates run_id on resume while preserving
    # activation_event_count/activated_at from the original forced action.
    state["run_id"] = "run-2"

    effective = provenance.apply_provenance_gate(forced, task, state)

    assert provenance._evidence_window_start(events, state) == 0
    assert effective["causal_evidence_targets"] == [causal]
    assert effective["action_kind"] == "edit"
    assert "coding_apply_patch" in effective["allowed_tools"]


def test_basename_only_repository_evidence_does_not_unlock_edit():
    causal = "services/gateway/app/static/image_catalog_ui.js"
    task = {
        "agent_events": [_started(), *_read_events(causal)],
        "project_plan": _plan("image_catalog_ui.js contains the failing gate"),
    }

    state = provenance.apply_provenance_gate(forced, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["causal_evidence_targets"] == [causal]
    assert state["hypothesis_causal_evidence_linked"] is False
    assert "coding_apply_patch" not in state["allowed_tools"]
