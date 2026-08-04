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


def test_forced_action_allows_only_edits_validation_diff_or_finish():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
    )

    allowed, _ = forced.evaluate_tool_call(task, name="coding_read_file_lines", args={"path": "x.py"}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_run_command", args={"argv": ["git", "log", "-1"]}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_run_command", args={"argv": ["python", "-m", "pytest", "-q"]}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True
    allowed, _ = forced.evaluate_tool_call(task, name="coding_replace_text", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True
    allowed, _ = forced.evaluate_tool_call(task, name="coding_git_diff", args={}, is_validation_command=coding_agent._is_validation_command)
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

    for rendered in (native_prompt, text_prompt, text_guidance):
        assert "coding_search_text" not in rendered
        assert "coding_read_file_lines" not in rendered
        assert "coding_update_plan" not in rendered
    assert "coding_git_diff" in text_prompt
    assert "coding_git_diff" in text_guidance
    assert all("coding_search_text" not in item for item in manifest["guidance"])
    assert set(manifest["tool_names"]) == forced.allowed_tool_names(task)
