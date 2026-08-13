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


def test_legacy_bounded_edit_resume_preserves_activation_history():
    task = _task()
    key = resilience.durable_state_key(task)
    required_action = "Make the smallest evidence-backed edit, or finish with a concrete blocker."

    legacy = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action=required_action,
        action_kind="bounded",
    )
    # Recreate the persisted v1 shape that predates directive-specific kind
    # normalization while retaining its original activation metadata.
    legacy["action_kind"] = "bounded"
    legacy["attempt_limit"] = 1
    legacy["activation_event_count"] = 7
    legacy["activation_count"] = 1
    legacy["resume_count"] = 0
    legacy["activated_at"] = 1234.5
    task["agent_forced_action"] = legacy

    resumed = forced.activate(
        task,
        state_key=key,
        run_id="run-3",
        cycle=1,
        stage="continuation",
        required_action=required_action,
        action_kind="bounded",
    )

    assert resumed["action_kind"] == "edit"
    assert resumed["resume_count"] == 1
    assert resumed["activation_count"] == 2
    assert resumed["activation_event_count"] == 7
    assert resumed["activated_at"] == 1234.5
    assert set(resumed["allowed_tools"]) == {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_finish",
    }
