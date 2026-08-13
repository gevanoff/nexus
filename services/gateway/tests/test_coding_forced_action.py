from __future__ import annotations

from app import coding_agent
from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience
from app import coding_workspace as workspace


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


def test_extracts_latest_concrete_commitment_from_model_notes():
    events = [
        {"type": "assistant", "content": "Let me inspect the helper again."},
        {"type": "assistant", "content": "I have enough evidence. I'll add the missing stale-category regression test now."},
        {"type": "assistant", "content": "Let me read the file one more time."},
    ]
    assert resilience.extract_concrete_commitment(events) == "Add the missing stale-category regression test now."


def test_forced_action_persists_across_unchanged_resume_and_expires_on_progress():
    task = _task()
    key = resilience.durable_state_key(task)
    first = forced.activate(task, state_key=key, run_id="run-2", cycle=6, stage="interrupt", required_action="Add the regression test.")
    task["agent_forced_action"] = first
    task["agent_run_id"] = "run-3"
    resumed = forced.activate(task, state_key=key, run_id="run-3", cycle=1, stage="continuation", required_action="Add the regression test.")
    task["agent_forced_action"] = resumed

    assert forced.active_state(task)["resume_count"] == 1
    task["agent_progress_state"]["observation"]["workspace_fingerprint"] = "edited"
    assert forced.active_state(task) == {}
    assert forced.retire_if_state_changed(task, state_key=resilience.durable_state_key(task)) is True
    assert task["agent_forced_action"]["status"] == "superseded"


def test_rejection_counter_resets_when_forced_state_changes_or_expires():
    task = _task()
    first_key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=first_key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
    )
    key, count = forced.rejection_counter_for_state("", 1, task)
    assert key == first_key
    assert count == 0
    key, count = forced.rejection_counter_for_state(key, 1, task)
    assert count == 1

    task["agent_progress_state"]["observation"]["workspace_fingerprint"] = "edited"
    key, count = forced.rejection_counter_for_state(key, 1, task)
    assert key == ""
    assert count == 0


def test_forced_action_scopes_tools_to_the_required_action():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_events"] = []
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
        action_kind="edit",
    )

    allowed, _ = forced.evaluate_tool_call(task, name="coding_read_file_lines", args={"path": "x.py"}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_run_command", args={"argv": ["python", "-m", "pytest", "-q"]}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_git_diff", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_replace_text", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True
    allowed, _ = forced.evaluate_tool_call(task, name="coding_finish", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True


def test_review_mission_defaults_do_not_require_changes_or_commit():
    mission = workspace.normalize_coding_mission(
        {
            "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
            "repo_url": "https://github.com/example/repo.git",
            "base_branch": "main",
            "branch_name": "review",
        }
    )
    assert mission["completion_policy"]["require_file_changes"] is False
    assert mission["completion_policy"]["require_commit_on_success"] is False


def test_fix_mission_still_requires_changes_and_commit():
    mission = workspace.normalize_coding_mission(
        {
            "prompt": "Fix the stale qualification bug and add a regression test.",
            "repo_url": "https://github.com/example/repo.git",
            "base_branch": "main",
            "branch_name": "fix",
        }
    )
    assert mission["completion_policy"]["require_file_changes"] is True
    assert mission["completion_policy"]["require_commit_on_success"] is True


def test_commitment_extraction_stops_before_transition_clause():
    events = [
        {
            "type": "assistant",
            "content": "I have enough evidence. I'll add the stale-category regression test before running pytest.",
        }
    ]

    assert resilience.extract_concrete_commitment(events) == "Add the stale-category regression test."


def test_mixed_review_and_fix_goal_still_requires_changes():
    mission = workspace.normalize_coding_mission(
        {
            "prompt": "Review this workspace and fix any bugs you find.",
            "repo_url": "https://github.com/example/repo.git",
            "base_branch": "main",
            "branch_name": "review-fix",
        }
    )

    assert mission["completion_policy"]["require_file_changes"] is True
    assert mission["completion_policy"]["require_commit_on_success"] is True


def test_publish_overrides_require_a_run_delta_and_commit():
    mission = workspace.coding_mission_overrides(push_on_success=True)

    assert mission["completion_policy"]["require_file_changes"] is True
    assert mission["completion_policy"]["require_commit_on_success"] is True


def test_forced_action_prompts_only_advertise_allowed_actions():
    task = _task()
    task.update(
        {
            "id": "code_forced_prompt",
            "prompt": "Add the regression test.",
            "base_branch": "main",
            "branch_name": "forced",
            "project_plan": {"items": []},
        }
    )
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
    )

    native_prompt = coding_agent._system_prompt(task)
    text_prompt = coding_agent._system_prompt(task, text_tool_mode=True)
    manifest = coding_agent.coding_tool_manifest(task)
    text_guidance = coding_agent._text_tool_call_guidance(task)
    retry_guidance = coding_agent._no_tool_call_guidance(
        task,
        malformed_text_tool_call=True,
        no_tool_cycles=2,
    )

    for rendered in (native_prompt, text_prompt, text_guidance, retry_guidance):
        assert "coding_search_text" not in rendered
        assert "coding_read_file_lines" not in rendered
        assert "coding_update_plan" not in rendered
    assert "coding_git_diff" not in text_prompt
    assert "coding_git_diff" not in text_guidance
    assert all("coding_search_text" not in item for item in manifest["guidance"])
    assert set(manifest["tool_names"]) == forced.allowed_tool_names(task)


def test_completed_commitment_is_not_reused_as_required_action():
    task = _task()
    task["mission"] = {"completion_policy": {"require_file_changes": True}}
    events = [
        {"type": "assistant", "content": "I'll run with the correct relative path.", "ts": 1},
        {"type": "tool_started", "name": "coding_run_command", "args": {"argv": ["python", "-m", "pytest", "tests/test_example.py"]}, "ts": 2},
        {"type": "tool_finished", "name": "coding_run_command", "result": {"ok": True}, "ts": 3},
        {"type": "assistant", "content": "The targeted test passed; I need to decide what follows.", "ts": 4},
    ]
    working = resilience.build_working_memory(
        task,
        state_key=resilience.durable_state_key(task),
        controller={"classification": "validation_loop", "stage": "interrupt"},
        ledger=[],
        events=events,
    )
    assert working["next_action"] != "Run with the correct relative path."
    assert working["next_action_kind"] == "edit"
    assert working["required_action_source"] == "controller_default"


def test_review_interrupt_resolves_to_finish_instead_of_stale_validation():
    task = _task()
    task["mission"] = {"completion_policy": {"require_file_changes": False}}
    events = [
        {"type": "assistant", "content": "I'll run with the correct relative path.", "ts": 1},
        {"type": "tool_started", "name": "coding_run_command", "args": {"argv": ["python", "-m", "pytest", "tests/test_example.py"]}, "ts": 2},
        {"type": "tool_finished", "name": "coding_run_command", "result": {"ok": False}, "ts": 3},
        {"type": "assistant", "content": "The failures may be environment-dependent and need to be reported carefully.", "ts": 4},
    ]
    working = resilience.build_working_memory(
        task,
        state_key=resilience.durable_state_key(task),
        controller={"classification": "validation_loop", "stage": "interrupt"},
        ledger=[],
        events=events,
    )
    assert working["next_action"].startswith("Call coding_finish")
    assert working["next_action_kind"] == "finish"
    assert "environment or configuration blockers" in working["next_action"]


def test_forced_action_remains_scoped_after_allowed_attempt_without_progress():
    task = _task()
    task["agent_events"] = []
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Run the targeted test.",
        action_kind="validate",
    )
    assert forced.allowed_tool_names(task) == {"coding_run_command", "coding_finish"}
    task["agent_events"].append({"type": "tool_finished", "name": "coding_run_command", "result": {"ok": False}})
    assert forced.active_state(task)["attempt_count"] == 1
    assert forced.allowed_tool_names(task) == {"coding_run_command", "coding_finish"}
    allowed, rejection = forced.evaluate_tool_call(
        task,
        name="coding_run_command",
        args={"argv": ["python", "-m", "pytest", "-q"]},
        is_validation_command=coding_agent._is_validation_command,
    )
    assert allowed is True
    assert rejection == {}
    assert "until durable progress" in forced.prompt_context(task)


def test_legacy_exhausted_state_does_not_collapse_to_finish_only():
    task = _task()
    task["agent_events"] = []
    key = resilience.durable_state_key(task)
    legacy = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    legacy["attempt_limit"] = 1
    task["agent_forced_action"] = legacy
    task["agent_events"].append({"type": "tool_finished", "name": "coding_run_command", "result": {"ok": False}})

    allowed = forced.allowed_tool_names(task)
    assert "coding_write_file" in allowed
    assert "coding_replace_text" in allowed
    assert "coding_apply_patch" in allowed
    assert "coding_run_command" in allowed
    assert "coding_git_diff" in allowed
    assert "coding_finish" in allowed


def test_unchanged_resume_keeps_action_class_available_after_prior_attempt():
    task = _task()
    task["agent_events"] = []
    key = resilience.durable_state_key(task)
    first = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Run the targeted test.",
        action_kind="validate",
    )
    task["agent_forced_action"] = first
    task["agent_events"].append({"type": "tool_finished", "name": "coding_run_command", "result": {"ok": False}})
    task["agent_run_id"] = "run-3"
    resumed = forced.activate(
        task,
        state_key=key,
        run_id="run-3",
        cycle=1,
        stage="continuation",
        required_action="Run the targeted test.",
        action_kind="validate",
    )
    task["agent_forced_action"] = resumed

    state = forced.active_state(task)
    assert state["resume_count"] == 1
    assert state["attempt_count"] == 1
    assert set(state["allowed_tools"]) == {"coding_run_command", "coding_finish"}


def test_pending_commitment_survives_until_a_tool_is_attempted():
    events = [
        {"type": "tool_finished", "name": "coding_read_file_lines", "result": {"ok": True}},
        {"type": "assistant", "content": "I have enough evidence. I'll add the regression test now."},
    ]
    assert resilience.pending_concrete_commitment(events) == "Add the regression test now."


def test_completed_commitment_expires_when_only_tool_finished_remains_after_rollover():
    events = [
        {"type": "assistant", "content": "I'll run with the correct relative path.", "ts": 1},
        {"type": "tool_finished", "name": "coding_run_command", "result": {"ok": True}, "ts": 2},
        {"type": "assistant", "content": "The targeted validation completed; I need to decide what follows.", "ts": 3},
    ]

    assert resilience.pending_concrete_commitment(events) == ""
