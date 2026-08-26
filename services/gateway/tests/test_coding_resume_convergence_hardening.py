from __future__ import annotations

import sys
from types import SimpleNamespace

import app

from app import coding_resume_convergence_hardening as hardening


class Policy:
    def __init__(self):
        self.base_state = {
            "schema": "base",
            "status": "active",
            "action_kind": "edit",
            "allowed_tools": ["coding_read_file_lines", "coding_replace_text"],
        }

    def active_state(self, _task):
        return dict(self.base_state)

    def allowed_tool_names(self, task):
        state = self.active_state(task)
        return set(state.get("allowed_tools") or [])

    def prompt_context(self, _task):
        return "BASE PROMPT"


class CW:
    def __init__(self, task):
        self.task = task

    def load_task(self, _task_id):
        return self.task


class MissionEpoch:
    KEY = "coding_mission_acceptance_epoch"
    REFUTATION_KEY = "coding_hypothesis_refutation"
    REFUTATION_SCHEMA = "nexus_coding_hypothesis_refutation.v1"

    @staticmethod
    def mission_delta_state(_cw, _task_id, task):
        return {
            "ok": True,
            "has_delta": True,
            "diff_sha256": str(task.get("test_diff_sha") or "diff-sha"),
        }


class Convergence:
    @staticmethod
    def _material_hypothesis_updated_at(task):
        return float(task.get("test_material_hypothesis_updated_at") or 0.0)

    @staticmethod
    def _readiness_threshold(task, _mission_epoch):
        epoch = task[MissionEpoch.KEY]
        lifecycle = task["agent_hypothesis_lifecycle"]
        return max(
            float(epoch.get("last_mutation_at") or 0.0),
            float(lifecycle.get("consumed_at") or 0.0),
            float(task.get("test_material_hypothesis_updated_at") or 0.0),
        )

    @staticmethod
    def _latest_decisive_rejection(task, _threshold):
        return {"accepted": False} if task.get("test_semantic_rejection") else {}

    @staticmethod
    def _semantic_rejection_guard_blocks(_cw, _mission_epoch, _task_id, task):
        return bool(task.get("test_rejection_guard"))

    @staticmethod
    def _validation_ready(task, _threshold):
        return bool(task.get("test_validation_ready")), float(task.get("test_validation_at") or 0.0)

    @staticmethod
    def _latest_diff_review_at(task, _threshold):
        return float(task.get("test_review_at") or 0.0)

    @staticmethod
    def _terminal_state(_cw, _mission_epoch, task):
        return {
            "schema": "nexus_coding_terminal_convergence.v1",
            "status": "active",
            "action_kind": "finish",
            "canonical_action_kind": "finish",
            "allowed_tools": ["coding_finish"],
            "required_action": "finish",
            "canonical_required_action": "finish",
            "state_key": f"finish:{task['id']}",
        }


def pending_task():
    return {
        "id": "code-resume",
        "project_plan": {"revision": 2, "updated_at": 5.0, "note": "same hypothesis"},
        "coding_mission_acceptance_epoch": {
            "schema": "nexus_coding_mission_acceptance_epoch.v1",
            "status": "pending",
            "last_mutation_at": 10.0,
        },
        "agent_hypothesis_lifecycle": {
            "schema": "nexus_coding_hypothesis_lifecycle.v1",
            "status": "consumed",
            "consumed_at": 10.0,
            "plan_revision": 2,
        },
    }


def installed(task):
    policy = Policy()
    agent = SimpleNamespace(forced_action=policy)
    cw = CW(task)
    hardening._install_policy(agent, cw, MissionEpoch, Convergence)
    return agent, cw


def test_resumed_pending_delta_forces_validation_before_broad_inspection():
    task = pending_task()
    task["test_validation_ready"] = False
    task["test_review_at"] = 12.0
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == hardening.SCHEMA
    assert state["action_kind"] == "validate"
    assert state["allowed_tools"] == ["coding_finish", "coding_run_command"]
    assert "coding_read_file_lines" not in state["allowed_tools"]
    assert "validation" in agent.forced_action.prompt_context(task).lower()


def test_resumed_pending_delta_forces_diff_review_after_validation():
    task = pending_task()
    task["test_validation_ready"] = True
    task["test_validation_at"] = 12.0
    task["test_review_at"] = 0.0
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == hardening.SCHEMA
    assert state["action_kind"] == "review"
    assert state["allowed_tools"] == ["coding_finish", "coding_git_diff"]
    assert "coding_replace_text" not in state["allowed_tools"]
    assert "coding_git_diff" in agent.forced_action.prompt_context(task)


def test_resumed_pending_delta_becomes_finish_only_when_prerequisites_are_current():
    task = pending_task()
    task["test_validation_ready"] = True
    task["test_validation_at"] = 12.0
    task["test_review_at"] = 13.0
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == "nexus_coding_terminal_convergence.v1"
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]


def test_active_refutation_is_not_overridden_by_post_edit_convergence():
    task = pending_task()
    task[MissionEpoch.REFUTATION_KEY] = {
        "schema": MissionEpoch.REFUTATION_SCHEMA,
        "status": "active",
    }
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == "base"
    assert state["action_kind"] == "edit"


def test_replacement_hypothesis_waits_for_consuming_edit_before_validation():
    task = pending_task()
    task["test_material_hypothesis_updated_at"] = 20.0
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == "base"
    assert state["action_kind"] == "edit"


def test_semantic_rejection_reopens_repair_instead_of_forcing_validation():
    task = pending_task()
    task["test_semantic_rejection"] = True
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == "base"
    assert state["action_kind"] == "edit"


def test_generic_failed_run_becomes_sentinel_auto_resume_blocker(monkeypatch):
    fake_sentinel = SimpleNamespace(_CODING_AUTO_RESUME_BLOCKERS={"finish_gate"})
    monkeypatch.setitem(sys.modules, "app.sentinel_runtime", fake_sentinel)
    monkeypatch.setattr(app, "sentinel_runtime", fake_sentinel, raising=False)

    hardening._install_sentinel_failed_resume_guard()

    assert "run_failed" in fake_sentinel._CODING_AUTO_RESUME_BLOCKERS
    assert "finish_gate" in fake_sentinel._CODING_AUTO_RESUME_BLOCKERS
