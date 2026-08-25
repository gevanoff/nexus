from __future__ import annotations

import asyncio

from app import coding_mission_acceptance_continuity as continuity


class _CW:
    def __init__(self):
        self.current_head = "checkpoint"
        self.base_head = "base"
        self.diff_text = "diff --git a/app.py b/app.py\n+fixed\n"
        self.dirty = False
        self.task = {
            "id": "code-test",
            "base_branch": "main",
            "agent_run_id": "run-a",
            "agent_start_head": "checkpoint",
            "last_commit": "checkpoint",
            "project_plan": {"items": []},
        }
        self.saved = []
        self.baselines = []
        self.coding_state_snapshot = lambda _task_id: {
            "changes": {"changed_files": [], "last_edit_at": 10.0},
            "validation": {
                "last_validation_ok": True,
                "last_validation_at": 20.0,
                "validation_after_latest_edit": True,
            },
            "diff_review": {
                "last_diff_review_at": 21.0,
                "diff_reviewed_after_latest_edit": True,
            },
            "progress": {
                "current_phase": "editing",
                "next_recommended_action": "continue the current project-plan milestone",
            },
        }

    def load_task(self, _task_id):
        return self.task

    def save_task(self, task):
        self.task = task
        self.saved.append(dict(task))
        return task

    def git_head(self, _task_id):
        return {"ok": True, "commit": self.current_head}

    def git_change_summary(self, _task_id):
        return {
            "ok": True,
            "counts": {"total": 1 if self.dirty else 0},
            "files": [],
        }

    def run_task_command(self, _task_id, *, argv, timeout_sec=None, **_kwargs):
        del timeout_sec
        if list(argv[:2]) == ["git", "merge-base"]:
            return {"ok": True, "stdout": self.base_head + "\n"}
        return {"ok": True, "stdout": ""}


class _RunDelta:
    SCHEMA = "nexus_coding_run_delta.v1"

    def __init__(self):
        self.baselines = []

    def run_delta_diff(self, cw, _agent, _task_id, task):
        baseline = dict(task.get("agent_semantic_baseline") or {})
        self.baselines.append(baseline)
        return cw.diff_text


class _Agent:
    def __init__(self, cw):
        self.cw = cw
        self.finalize_seen_start_heads = []
        self.start_calls = 0

    async def start_agent_run(self, task_id, *args, **kwargs):
        del task_id, args, kwargs
        self.start_calls += 1
        return {"ok": True}

    @staticmethod
    def _mission_requires_workspace_edits(_task):
        return True

    def finalize_successful_run(self, task_id, *args, **kwargs):
        del args, kwargs
        self.finalize_seen_start_heads.append(self.cw.load_task(task_id)["agent_start_head"])
        return {"ok": True, "final_commit": self.cw.current_head}


class _Guarded:
    def __init__(self):
        self._run_delta_diff = lambda *_args, **_kwargs: "old-run-delta"


class _ForcedAction:
    def __init__(self):
        self._ACTION_ALLOWED_TOOLS = {
            "edit": {"coding_write_file", "coding_finish"},
        }
        self.state = {
            "action_kind": "edit",
            "state_key": "state-1",
            "required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        }

    def active_state(self, _task):
        return dict(self.state)

    @staticmethod
    def evaluate_tool_call(_task, *, name, args, is_validation_command):
        del name, args, is_validation_command
        return False, {"error": "original"}

    @staticmethod
    def prompt_context(_task):
        return "forced action context"


def test_acceptance_epoch_uses_base_merge_base_not_checkpoint_head():
    cw = _CW()

    epoch = continuity.ensure_acceptance_epoch(cw, "code-test")

    assert epoch["base_head"] == "base"
    assert epoch["base_head"] != cw.current_head
    assert cw.task[continuity.KEY]["status"] == "pending"


def test_mission_delta_keeps_same_base_across_resumed_run_ids():
    cw = _CW()
    run_delta = _RunDelta()
    agent = _Agent(cw)

    first = continuity.mission_delta_diff(cw, agent, run_delta, "code-test", cw.task)
    cw.task["agent_run_id"] = "run-b"
    second = continuity.mission_delta_diff(cw, agent, run_delta, "code-test", cw.task)

    assert first == second == cw.diff_text
    assert [item["tree_commit"] for item in run_delta.baselines] == ["base", "base"]
    assert [item["run_id"] for item in run_delta.baselines] == ["run-a", "run-b"]


def test_resumed_checkpoint_can_finish_after_mission_level_acceptance():
    cw = _CW()
    run_delta = _RunDelta()
    agent = _Agent(cw)
    guarded = _Guarded()
    forced = _ForcedAction()

    continuity.install(agent, guarded, cw, run_delta, forced)

    # This is the debug-report failure mode: the resumed run starts at the
    # checkpoint commit, so there is no run-local commit delta, but the mission
    # still has an unaccepted delta relative to its base branch.
    assert cw.current_head == cw.task["agent_start_head"] == "checkpoint"
    assert agent._mission_requires_workspace_edits(cw.task) is False
    assert guarded._run_delta_diff("code-test", cw.task) == cw.diff_text

    result = agent.finalize_successful_run("code-test")

    assert result["ok"] is True
    assert agent.finalize_seen_start_heads == ["base"]
    assert cw.task["agent_start_head"] == "checkpoint"
    assert cw.task[continuity.KEY]["status"] == "accepted"
    assert cw.task[continuity.KEY]["accepted_head"] == "checkpoint"


def test_same_run_changes_still_require_normal_edit_contract():
    cw = _CW()
    run_delta = _RunDelta()
    agent = _Agent(cw)
    guarded = _Guarded()
    forced = _ForcedAction()
    continuity.install(agent, guarded, cw, run_delta, forced)

    cw.current_head = "new-checkpoint"

    assert agent._mission_requires_workspace_edits(cw.task) is True


def test_snapshot_reports_inherited_mission_delta_as_finalizable():
    cw = _CW()
    run_delta = _RunDelta()
    agent = _Agent(cw)
    guarded = _Guarded()
    forced = _ForcedAction()
    continuity.install(agent, guarded, cw, run_delta, forced)

    snapshot = cw.coding_state_snapshot("code-test")

    assert snapshot["mission_acceptance"]["base_head"] == "base"
    assert snapshot["mission_acceptance"]["current_head"] == "checkpoint"
    assert snapshot["mission_acceptance"]["has_delta"] is True
    assert snapshot["progress"]["current_phase"] == "finalizing"
    assert snapshot["progress"]["next_recommended_action"] == "finish the mission"


def test_empty_plan_without_mission_delta_does_not_recommend_nonexistent_milestone():
    cw = _CW()
    cw.diff_text = ""
    run_delta = _RunDelta()
    agent = _Agent(cw)
    guarded = _Guarded()
    forced = _ForcedAction()
    continuity.install(agent, guarded, cw, run_delta, forced)

    snapshot = cw.coding_state_snapshot("code-test")

    assert snapshot["mission_acceptance"]["has_delta"] is False
    assert snapshot["progress"]["next_recommended_action"] == (
        "establish a remediation hypothesis or concrete blocker"
    )


def test_forced_edit_mode_only_allows_plan_update_for_explicit_refutation():
    cw = _CW()
    run_delta = _RunDelta()
    agent = _Agent(cw)
    guarded = _Guarded()
    forced = _ForcedAction()
    continuity.install(agent, guarded, cw, run_delta, forced)

    assert "coding_update_plan" in forced._ACTION_ALLOWED_TOOLS["edit"]

    allowed, rejection = forced.evaluate_tool_call(
        cw.task,
        name="coding_update_plan",
        args={"note": "I want to rethink the plan."},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is False
    assert rejection["error"] == "forced_action_tool_rejected"
    assert "Hypothesis refuted:" in rejection["message"]

    allowed, rejection = forced.evaluate_tool_call(
        cw.task,
        name="coding_update_plan",
        args={
            "note": (
                "Hypothesis refuted: verified frontend evidence already renders management.ui_url, "
                "so the current frontend-missing-link explanation is false."
            )
        },
        is_validation_command=lambda _argv: False,
    )
    assert allowed is True
    assert rejection == {}
    assert "Hypothesis refutation escape hatch" in forced.prompt_context(cw.task)


def test_start_agent_run_initializes_acceptance_epoch_before_execution():
    cw = _CW()
    cw.task.pop(continuity.KEY, None)
    run_delta = _RunDelta()
    agent = _Agent(cw)
    guarded = _Guarded()
    forced = _ForcedAction()
    continuity.install(agent, guarded, cw, run_delta, forced)

    result = asyncio.run(agent.start_agent_run("code-test"))

    assert result["ok"] is True
    assert agent.start_calls == 1
    assert cw.task[continuity.KEY]["base_head"] == "base"
