from __future__ import annotations

from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience


def _task() -> dict:
    return {
        "agent_run_id": "run-evidence",
        "agent_cycle": 6,
        "agent_events": [],
        "agent_progress_state": {
            "stagnant_cycles": 6,
            "observation": {
                "workspace_fingerprint": "same",
                "validation_revision": 0,
                "diff_review_revision": 0,
                "finish_state": "running",
            },
        },
    }


def test_decision_evidence_directive_allows_source_inspection_but_not_edits() -> None:
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-evidence",
        cycle=6,
        stage="interrupt",
        required_action="Gather one targeted piece of repository evidence before deciding whether to edit.",
        action_kind="bounded",
    )

    state = forced.active_state(task)
    assert state["action_kind"] == "evidence"
    assert set(state["allowed_tools"]) == {
        "coding_read_file",
        "coding_read_file_lines",
        "coding_search_text",
        "coding_finish",
    }

    for name in ("coding_read_file", "coding_read_file_lines", "coding_search_text"):
        allowed, rejection = forced.evaluate_tool_call(
            task,
            name=name,
            args={"path": "services/gateway/app/example.py", "query": "needle"},
            is_validation_command=lambda _argv: False,
        )
        assert allowed is True
        assert rejection == {}

    for name in ("coding_write_file", "coding_replace_text", "coding_apply_patch", "coding_run_command"):
        allowed, rejection = forced.evaluate_tool_call(
            task,
            name=name,
            args={},
            is_validation_command=lambda _argv: True,
        )
        assert allowed is False
        assert rejection["error"] == "forced_action_tool_rejected"


def test_persisted_bounded_evidence_directive_normalizes_before_resume_schema() -> None:
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = {
        "schema": forced.SCHEMA,
        "status": "active",
        "state_key": key,
        "run_id": "run-old",
        "cycle": 6,
        "stage": "interrupt",
        "required_action": "Gather one targeted piece of repository evidence before deciding whether to edit.",
        "action_kind": "bounded",
        "allowed_tools": ["coding_write_file", "coding_finish"],
        "activation_event_count": 0,
        "activation_count": 1,
        "resume_count": 0,
        "activated_at": 1.0,
    }

    state = forced.active_state(task)

    assert state["action_kind"] == "evidence"
    assert "coding_search_text" in state["allowed_tools"]
    assert "coding_write_file" not in state["allowed_tools"]
