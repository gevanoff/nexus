from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

import app
from app import coding_acceptance_convergence_hardening as real_convergence
from app import coding_resume_convergence_hardening as hardening
from app import coding_terminal_acceptance_hardening as terminal
from app import coding_work_phases

_VALIDATION_KEY = hardening._VALIDATION_KEY


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

    def save_task(self, task):
        self.task = task
        return task

    def mutate_task(self, _task_id, mutator):
        mutator(self.task)
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
    def _validation_records_from_history(task, _threshold):
        return list(task.get("test_validation_records") or [])

    @staticmethod
    def _validation_records_from_events(_task, _threshold):
        return []

    @staticmethod
    def _validation_records_from_commands(_task, _threshold):
        return []

    @staticmethod
    def _unresolved_validation_failures(task, _threshold):
        records = list(task.get("test_validation_records") or [])
        failures = []
        for failed_at, failed_signature, ok in records:
            if ok:
                continue
            superseded = any(
                later_at > failed_at and later_signature == failed_signature and later_ok
                for later_at, later_signature, later_ok in records
            )
            if not superseded:
                failures.append((failed_at, failed_signature))
        return failures

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


def test_failed_validation_opens_governed_repair_instead_of_validate_livelock():
    task = pending_task()
    task["test_validation_ready"] = False
    task["test_validation_at"] = 12.0
    task["test_validation_records"] = [
        (12.0, ("pytest", "-q"), False),
        (13.0, ("git", "diff", "--check"), True),
    ]
    agent, _cw = installed(task)
    state = agent.forced_action.active_state(task)
    assert state["schema"] == hardening.SCHEMA
    assert state["stage"] == "post_edit_validation_repair"
    assert state["action_kind"] == "edit"
    assert state["validation_repair"] is True
    assert "pytest -q" in state["unresolved_validation_signatures"]
    assert "coding_replace_text" in state["allowed_tools"]
    assert "coding_apply_patch" in state["allowed_tools"]
    assert "coding_update_plan" in state["allowed_tools"]
    assert "coding_run_command" in state["allowed_tools"]
    assert "coding_refute_hypothesis" in state["allowed_tools"]
    assert "weaker green check" in state["required_action"]
    assert "explicitly available" in state["required_action"]


def test_same_failed_signature_success_clears_repair_obligation():
    task = pending_task()
    task["test_validation_ready"] = True
    task["test_validation_at"] = 14.0
    task["test_validation_records"] = [
        (12.0, ("pytest", "-q"), False),
        (14.0, ("pytest", "-q"), True),
    ]
    task["test_review_at"] = 15.0
    agent, _cw = installed(task)
    state = agent.forced_action.active_state(task)
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]


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


def test_harness_pending_delta_defers_validation_and_forces_diff_review():
    task = pending_task()
    task["kind"] = "harness_eval"
    task["test_validation_ready"] = False
    task["test_validation_at"] = 12.0
    task["test_validation_records"] = [
        (12.0, ("pytest", "-q"), False),
    ]
    task["test_review_at"] = 0.0
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == hardening.SCHEMA
    assert state["action_kind"] == "review"
    assert state["allowed_tools"] == ["coding_finish", "coding_git_diff"]
    assert state["declared_validation_deferred"] is True
    assert "coding_run_command" not in state["allowed_tools"]
    prompt = agent.forced_action.prompt_context(task).lower()
    assert "trusted runner" in prompt
    assert "coding_git_diff" in prompt


def test_harness_pending_delta_reaches_finish_after_diff_review():
    task = pending_task()
    task["kind"] = "harness_eval"
    task["test_validation_ready"] = False
    task["test_review_at"] = 13.0
    agent, _cw = installed(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == "nexus_coding_terminal_convergence.v1"
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]


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


def test_sentinel_policy_drift_is_repaired_without_boot_failure(monkeypatch):
    fake_sentinel = SimpleNamespace(_CODING_AUTO_RESUME_BLOCKERS=frozenset({"finish_gate"}))
    monkeypatch.setitem(sys.modules, "app.sentinel_runtime", fake_sentinel)
    monkeypatch.setattr(app, "sentinel_runtime", fake_sentinel, raising=False)
    hardening._install_sentinel_failed_resume_guard()
    blockers = fake_sentinel._CODING_AUTO_RESUME_BLOCKERS
    assert isinstance(blockers, set)
    assert "run_failed" in blockers
    assert "finish_gate" in blockers


def test_sentinel_declares_run_failed_blocker_in_its_own_literal():
    from app import sentinel_runtime

    assert "run_failed" in sentinel_runtime._CODING_AUTO_RESUME_BLOCKERS


def test_install_requires_acceptance_convergence_first():
    policy = Policy()
    agent = SimpleNamespace(forced_action=policy, _run_tool=lambda *a, **k: {})
    with pytest.raises(RuntimeError, match="acceptance_convergence_hardening"):
        hardening.install(agent, CW({}), MissionEpoch, Convergence)


def test_structured_hypothesis_identity_ignores_arbitrary_trailing_plan_sections():
    note = (
        "Root cause: typo\n"
        "Repository evidence: app.py:10\n"
        "Competing explanation checked: cache\n"
        "Expected result: ok\n"
        "Status (auto): waiting\n"
        "状態: 実行待ち"
    )
    fields = real_convergence._structured_hypothesis_fields(note)
    assert fields == {
        "Root cause": "typo",
        "Repository evidence": "app.py:10",
        "Competing explanation checked": "cache",
        "Expected result": "ok",
    }


def test_validation_event_pairing_does_not_steal_idless_validation_for_unmatched_id():
    task = {
        "agent_events": [
            {
                "type": "tool_started",
                "name": "coding_run_command",
                "ts": 11.0,
                "args": {"argv": ["pytest", "-q"]},
            },
            {
                "type": "tool_finished",
                "name": "coding_run_command",
                "tool_call_id": "unmatched-nonvalidation",
                "ts": 11.1,
                "result": {"ok": False, "stderr": "cat: missing"},
            },
            {
                "type": "tool_finished",
                "name": "coding_run_command",
                "ts": 11.2,
                "result": {"ok": True},
            },
        ]
    }
    records = real_convergence._validation_records_from_events(task, 10.0)
    assert records == [(11.2, ("pytest", "-q"), True)]


def test_nonvalidation_command_does_not_pollute_validation_history():
    task = {
        "id": "code-history",
        "agent_run_id": "run-1",
        "agent_cycle": 1,
        "commands": [
            {"label": "agent-command", "argv": ["pytest", "-q"], "ok": True, "ts": 11.0},
        ],
    }
    cw = CW(task)
    real_convergence._install_validation_continuity()
    terminal._persist_validation_provenance(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["pytest", "-q"],
        cwd="",
        result={"ok": True},
    )
    before = list(task[_VALIDATION_KEY]["history"])
    task["commands"].append(
        {"label": "agent-command", "argv": ["cat", "missing-file"], "ok": False, "ts": 12.0}
    )
    terminal._persist_validation_provenance(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["cat", "missing-file"],
        cwd="",
        result={"ok": False, "stderr": "cat: missing-file: No such file"},
    )
    assert task[_VALIDATION_KEY]["argv"] == ["pytest", "-q"]
    assert task[_VALIDATION_KEY]["history"] == before


def test_validation_side_effect_is_restamped_after_mission_mutation():
    task = {
        "id": "code-side-effect",
        "coding_mission_acceptance_epoch": {
            "schema": "nexus_coding_mission_acceptance_epoch.v1",
            "status": "pending",
            "last_mutation_at": 20.0,
        },
        _VALIDATION_KEY: {
            "schema": "nexus_coding_validation_provenance.v1",
            "argv": ["pytest", "-q"],
            "ok": True,
            "ts": 19.0,
            "history": [
                {"argv": ["pytest", "-q"], "ok": True, "ts": 19.0, "substantive": True}
            ],
        },
    }
    cw = CW(task)
    hardening._restamp_validation_after_workspace_mutation(
        cw,
        MissionEpoch,
        task["id"],
        ["pytest", "-q"],
    )
    assert task[_VALIDATION_KEY]["ts"] > 20.0
    assert task[_VALIDATION_KEY]["history"][-1]["ts"] == task[_VALIDATION_KEY]["ts"]


class TrackedDeltaMissionEpoch:
    KEY = "coding_mission_acceptance_epoch"

    def __init__(self, tracked_shas):
        self._tracked = list(tracked_shas)

    def mission_delta_state(self, _cw, _task_id, task=None):
        sha = self._tracked.pop(0) if len(self._tracked) > 1 else self._tracked[0]
        return {
            "ok": True,
            "has_delta": True,
            "diff_sha256": f"combined-{sha}",
            "tracked_diff_sha256": sha,
        }


def _restamp_wrapper_run(monkeypatch, tracked_shas):
    from app import coding_agent_guarded as guarded

    task = {
        "id": "code-side-effect",
        "coding_mission_acceptance_epoch": {
            "schema": "nexus_coding_mission_acceptance_epoch.v1",
            "status": "pending",
            "last_mutation_at": 20.0,
        },
        _VALIDATION_KEY: {
            "schema": "nexus_coding_validation_provenance.v1",
            "argv": ["pytest", "-q"],
            "ok": True,
            "ts": 19.0,
            "history": [
                {"argv": ["pytest", "-q"], "ok": True, "ts": 19.0, "substantive": True}
            ],
        },
    }
    monkeypatch.setattr(
        guarded,
        "_run_tool_with_semantic_acceptance",
        guarded._run_tool_with_semantic_acceptance,
        raising=False,
    )
    cw = CW(task)
    agent = SimpleNamespace(
        forced_action=Policy(),
        _run_tool=lambda task_id, name, args, *, git_token_value: {
            "ok": True,
            "workspace_modified": True,
        },
    )
    hardening._install_validation_side_effect_restamp(
        agent, cw, TrackedDeltaMissionEpoch(tracked_shas)
    )
    agent._run_tool(
        task["id"], "coding_run_command", {"argv": ["pytest", "-q"]}, git_token_value=None
    )
    return task


def test_untracked_only_validation_side_effect_is_restamped(monkeypatch):
    task = _restamp_wrapper_run(monkeypatch, ["tracked-A", "tracked-A"])
    assert task[_VALIDATION_KEY]["ts"] > 20.0


def test_tracked_source_mutation_by_validation_is_not_restamped(monkeypatch):
    task = _restamp_wrapper_run(monkeypatch, ["tracked-A", "tracked-B"])
    # The validation rewrote tracked source (fix-mode linter, snapshot update):
    # its provenance must stay stale so validation re-runs on the produced tree.
    assert task[_VALIDATION_KEY]["ts"] == 19.0


class RealMissionEpoch:
    KEY = "coding_mission_acceptance_epoch"

    @staticmethod
    def mission_delta_state(_cw, _task_id, _task):
        return {"ok": True, "has_delta": True, "diff_sha256": "same-diff"}


def test_real_terminal_state_respects_durable_rejection_guard_without_event():
    task = {
        "id": "code-guard",
        "project_plan": {"revision": 2, "note": "status only"},
        "coding_mission_acceptance_epoch": {
            "schema": "nexus_coding_mission_acceptance_epoch.v1",
            "status": "pending",
            "last_mutation_at": 10.0,
        },
        "agent_hypothesis_lifecycle": {
            "schema": "nexus_coding_hypothesis_lifecycle.v1",
            "status": "consumed",
            "consumed_at": 10.0,
            "verified_evidence_digest": "evidence-A",
        },
        "coding_validation_provenance": {
            "schema": "nexus_coding_validation_provenance.v1",
            "argv": ["pytest", "-q"],
            "ok": True,
            "ts": 12.0,
        },
        "agent_events": [
            {"type": "tool_finished", "name": "coding_git_diff", "ts": 13.0, "result": {"ok": True}}
        ],
    }
    cw = CW(task)
    key = real_convergence._semantic_rejection_guard_key(
        cw,
        RealMissionEpoch,
        task["id"],
        task,
    )
    task["coding_semantic_rejection_guard"] = {"causal_key": key}
    state = real_convergence._terminal_state(cw, RealMissionEpoch, task)
    assert state == {}
