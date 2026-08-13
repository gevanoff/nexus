from __future__ import annotations

from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience


def _task() -> dict:
    return {
        "agent_run_id": "run-2",
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


def test_execution_default_edit_directive_is_not_left_bounded():
    task = _task()
    state = forced.activate(
        task,
        state_key=resilience.durable_state_key(task),
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    task["agent_forced_action"] = state

    assert state["action_kind"] == "edit"
    assert forced.allowed_tool_names(task) == {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_finish",
    }
