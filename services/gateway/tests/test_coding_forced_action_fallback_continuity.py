from __future__ import annotations

from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience
from app.prompt_canonicalization import canonicalize_chat_payload


def _task() -> dict:
    return {
        "agent_run_id": "run-2",
        "agent_cycle": 6,
        "project_plan": {"revision": 0, "goal": "repair", "items": [], "note": ""},
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


def _qualify_hypothesis(task: dict) -> None:
    state = forced.active_state(task)
    task.setdefault("agent_events", []).append(
        {
            "type": "tool_finished",
            "name": "coding_search_text",
            "result": {"ok": True, "matches": ["services/gateway/app/example.py:42"]},
            "ts": float(state.get("activated_at") or 0) + 1,
        }
    )
    task["project_plan"] = {
        "revision": int(state.get("activation_plan_revision") or 0) + 1,
        "goal": "repair",
        "items": [],
        "note": (
            "Root cause: The existing configured management-link path is being bypassed by the proposed static UI edit.\n"
            "Repository evidence: services/gateway/app/static/image_catalog_ui.js already renders model_management.ui_url.\n"
            "Competing explanation checked: The Image UI markup still contains the modelManagement container used by that renderer.\n"
            "Expected result: Fixing the configured catalog path restores the link without a hard-coded localhost address."
        ),
    }


def test_generic_execution_interrupt_enters_evidence_gate_before_edit_scope():
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

    assert active["canonical_required_action"] == "Make the smallest evidence-backed edit, or finish with a concrete blocker."
    assert active["canonical_action_kind"] == "edit"
    assert active["action_kind"] == "evidence"
    assert set(active["allowed_tools"]) == {
        "coding_search_text",
        "coding_read_file_lines",
        "coding_update_plan",
        "coding_finish",
    }


def test_evidence_and_new_structured_hypothesis_unlock_edit_scope():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )

    _qualify_hypothesis(task)
    active = forced.active_state(task)

    assert active["hypothesis_ready"] is True
    assert active["targeted_evidence_count"] == 1
    assert active["action_kind"] == "edit"
    assert set(active["allowed_tools"]) == {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_finish",
    }


def test_stale_structured_plan_cannot_unlock_new_forced_action():
    task = _task()
    task["project_plan"] = {
        "revision": 4,
        "goal": "repair",
        "items": [],
        "note": (
            "Root cause: old root cause from a previous state.\n"
            "Repository evidence: old.py:1 contained old behavior.\n"
            "Competing explanation checked: previous alternative was checked.\n"
            "Expected result: previous expected result was recorded."
        ),
    }
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    state = forced.active_state(task)
    task["agent_events"] = [
        {
            "type": "tool_finished",
            "name": "coding_read_file_lines",
            "result": {"ok": True},
            "ts": float(state.get("activated_at") or 0) + 1,
        }
    ]

    active = forced.active_state(task)

    assert active["targeted_evidence_count"] == 1
    assert active["hypothesis_ready"] is False
    assert active["action_kind"] == "evidence"


def test_targeted_evidence_is_bounded_until_hypothesis_is_recorded():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    state = forced.active_state(task)
    activated = float(state.get("activated_at") or 0)
    task["agent_events"] = [
        {"type": "tool_finished", "name": "coding_search_text", "result": {"ok": True}, "ts": activated + 1},
        {"type": "tool_finished", "name": "coding_read_file_lines", "result": {"ok": True}, "ts": activated + 2},
    ]

    active = forced.active_state(task)

    assert active["targeted_evidence_count"] == 2
    assert set(active["allowed_tools"]) == {"coding_update_plan", "coding_finish"}


def test_unchanged_resume_preserves_evidence_gate_activation_baseline():
    task = _task()
    key = resilience.durable_state_key(task)
    first = forced.activate(
        task,
        state_key=key,
        run_id="run-1",
        cycle=9,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    task["agent_forced_action"] = first
    first_activation = first["activated_at"]
    first_plan_revision = first["activation_plan_revision"]
    task["agent_run_id"] = "run-2"

    resumed = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=1,
        stage="continuation",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    task["agent_forced_action"] = resumed

    assert resumed["resume_count"] == 1
    assert resumed["activated_at"] == first_activation
    assert resumed["activation_plan_revision"] == first_plan_revision
    assert resumed["canonical_action_kind"] == "edit"
    assert resumed["action_kind"] == "evidence"


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

    assert active["canonical_required_action"] == "Make the smallest evidence-backed edit, or finish with a concrete blocker."
    assert active["canonical_action_kind"] == "edit"
    assert active["action_kind"] == "evidence"
    assert "coding_update_plan" in active["allowed_tools"]
    assert "coding_replace_text" not in active["allowed_tools"]


def test_specific_concrete_edit_action_does_not_reopen_investigation():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the stale-category regression test already identified in tests/test_model_tool_qualification.py.",
        action_kind="edit",
    )

    active = forced.active_state(task)

    assert active["requires_hypothesis"] is False
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
