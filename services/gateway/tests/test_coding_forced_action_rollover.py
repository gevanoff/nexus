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


def test_attempt_count_uses_timestamps_after_event_buffer_rollover():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_events"] = [
        {
            "type": "tool_finished",
            "name": "coding_run_command",
            "result": {"ok": True},
            "ts": 90.0 + index / 1000,
        }
        for index in range(1000)
    ]
    state = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Run the targeted test.",
        action_kind="validate",
    )
    state["activated_at"] = 100.0
    state["activation_event_count"] = 1000
    task["agent_forced_action"] = state

    task["agent_events"] = task["agent_events"][1:] + [
        {
            "type": "tool_finished",
            "name": "coding_run_command",
            "result": {"ok": False},
            "ts": 101.0,
        }
    ]

    active = forced.active_state(task)
    assert active["attempt_count"] == 1
    assert forced.allowed_tool_names(task) == {"coding_finish"}


def test_pre_activation_events_do_not_exhaust_attempt_after_rollover():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_events"] = [
        {
            "type": "tool_finished",
            "name": "coding_run_command",
            "result": {"ok": True},
            "ts": 99.0,
        }
    ]
    state = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Run the targeted test.",
        action_kind="validate",
    )
    state["activated_at"] = 100.0
    state["activation_event_count"] = 1000
    task["agent_forced_action"] = state

    active = forced.active_state(task)
    assert active["attempt_count"] == 0
    assert forced.allowed_tool_names(task) == {"coding_run_command", "coding_finish"}


def test_unknown_action_kind_is_normalized_to_bounded():
    task = _task()
    state = forced.activate(
        task,
        state_key=resilience.durable_state_key(task),
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded action.",
        action_kind="unexpected_kind",
    )
    task["agent_forced_action"] = state

    assert state["action_kind"] == "bounded"
    assert forced.allowed_tool_names(task) == {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_run_command",
        "coding_git_diff",
        "coding_finish",
    }
    assert "unexpected_kind" not in forced.prompt_context(task)
