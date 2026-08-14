from __future__ import annotations

from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience
from app.prompt_canonicalization import canonicalize_chat_payload


def _task() -> dict:
    return {
        "agent_run_id": "run-2",
        "agent_cycle": 6,
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


def test_generic_execution_interrupt_normalizes_to_edit_scope():
    task = _task()
    state = forced.activate(
        task,
        state_key=resilience.durable_state_key(task),
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )

    task["agent_forced_action"] = state
    active = forced.active_state(task)

    assert active["required_action"] == "Make the smallest evidence-backed edit, or finish with a concrete blocker."
    assert active["action_kind"] == "edit"
    assert set(active["allowed_tools"]) == {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_finish",
    }


def test_persisted_generic_bounded_state_normalizes_before_first_resumed_call():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = {
        "schema": forced.SCHEMA,
        "status": "active",
        "state_key": key,
        "run_id": "run-1",
        "cycle": 9,
        "stage": "interrupt",
        "required_action": "Take one bounded execution action, or finish with a concrete blocker.",
        "action_kind": "bounded",
        "allowed_tools": [
            "coding_write_file",
            "coding_replace_text",
            "coding_apply_patch",
            "coding_run_command",
            "coding_git_diff",
            "coding_finish",
        ],
        "attempt_limit": 0,
        "activation_event_count": 0,
        "activation_count": 1,
        "resume_count": 0,
        "rejection_limit": 2,
        "activated_at": 1.0,
        "updated_at": 1.0,
    }

    active = forced.active_state(task)

    assert active["required_action"] == "Make the smallest evidence-backed edit, or finish with a concrete blocker."
    assert active["action_kind"] == "edit"
    assert set(active["allowed_tools"]) == {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_finish",
    }


def test_canonicalization_bridges_tool_result_before_reroute_user_turn():
    payload = {
        "model": "coder",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "repair the workspace"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "coding_run_command", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"ok":false,"error":"forced_action_tool_rejected"}',
            },
            {
                "role": "user",
                "content": "The coding backend ignored enforced forced-action tool policy. Rerouting now.",
            },
        ],
    }

    out = canonicalize_chat_payload(payload)
    roles = [message["role"] for message in out["messages"]]

    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert out["messages"][-2] == {"content": "", "role": "assistant"}
    assert payload["messages"][-2]["role"] == "tool"


def test_canonicalization_keeps_normal_tool_completion_unchanged():
    payload = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "coding_apply_patch", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
            {"role": "assistant", "content": "Patch applied."},
        ]
    }

    out = canonicalize_chat_payload(payload)

    assert [message["role"] for message in out["messages"]] == ["assistant", "tool", "assistant"]
