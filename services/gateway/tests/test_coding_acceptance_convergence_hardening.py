from __future__ import annotations

import hashlib
from types import SimpleNamespace

from app import coding_acceptance_convergence_hardening as hardening


HYPOTHESIS_A = (
    "Root cause: existing causal path A\n"
    "Repository evidence: app.py:10-20\n"
    "Competing explanation checked: path B was ruled out\n"
    "Expected result: behavior A is restored"
)
HYPOTHESIS_B = (
    "Root cause: replacement causal path B\n"
    "Repository evidence: app.py:30-40\n"
    "Competing explanation checked: path A was contradicted\n"
    "Expected result: behavior B is restored"
)


class Policy:
    def __init__(self, state=None):
        self.state = dict(state or {})

    def active_state(self, _task):
        return dict(self.state)

    def allowed_tool_names(self, task):
        return set(self.active_state(task).get("allowed_tools") or [])

    def prompt_context(self, _task):
        return "BASE POLICY PROMPT"


class CW:
    def __init__(self, task):
        self.task = task

    def load_task(self, _task_id):
        return self.task


class MissionEpoch:
    KEY = "coding_mission_acceptance_epoch"
    REFUTATION_KEY = "coding_hypothesis_refutation"
    REFUTATION_SCHEMA = "nexus_coding_hypothesis_refutation.v1"
    REFUTATION_TOOL = "coding_refute_hypothesis"

    @staticmethod
    def mission_delta_state(_cw, _task_id, _task):
        return {
            "ok": True,
            "has_delta": True,
            "diff_sha256": "mission-diff-sha",
        }

    @staticmethod
    def _record_refutation(agent, cw, task_id, *, reason, contradicting_evidence, forced_state):
        del task_id
        previous = cw.task.get(MissionEpoch.REFUTATION_KEY) or {}
        count = int(previous.get("count") or 0) + 1
        record = {
            "schema": MissionEpoch.REFUTATION_SCHEMA,
            "status": "active",
            "count": count,
            "reason": reason,
            "contradicting_evidence": contradicting_evidence,
            "state_key": forced_state.get("state_key"),
        }
        cw.task[MissionEpoch.REFUTATION_KEY] = record
        agent.events.append({"type": "hypothesis_refuted", "count": count})
        return record


class SemanticAcceptance:
    def __init__(self):
        self._coding_acceptance_evidence_instruction_installed = False

    @staticmethod
    def build_review_messages(**_kwargs):
        return "BASE REVIEW SYSTEM", "BASE REVIEW USER"


def terminal_ready_task():
    return {
        "id": "code-terminal",
        "project_plan": {
            "revision": 2,
            "updated_at": 5.0,
            "note": HYPOTHESIS_A,
            "items": [],
        },
        "coding_mission_acceptance_epoch": {
            "schema": "nexus_coding_mission_acceptance_epoch.v1",
            "status": "pending",
            "last_mutation_at": 10.0,
        },
        "agent_hypothesis_lifecycle": {
            "status": "consumed",
            "consumed_at": 10.0,
            "plan_revision": 2,
            "note_fingerprint": hashlib.sha256(HYPOTHESIS_A.encode("utf-8")).hexdigest(),
        },
        "coding_validation_provenance": {
            "schema": "nexus_coding_validation_provenance.v1",
            "ok": True,
            "ts": 20.0,
        },
        "agent_events": [
            {
                "type": "tool_finished",
                "name": "coding_git_diff",
                "ts": 21.0,
                "result": {"ok": True},
            }
        ],
    }


def install_hardening(task, state=None):
    policy = Policy(state)
    calls = []
    agent = SimpleNamespace(
        forced_action=policy,
        events=[],
        _run_tool=lambda task_id, name, args, *, git_token_value: (
            calls.append((task_id, name, args, git_token_value))
            or {"ok": False, "error": "stale_handler_called"}
        ),
    )
    guarded = SimpleNamespace(_run_tool_with_semantic_acceptance=agent._run_tool)
    cw = CW(task)
    semantic = SemanticAcceptance()
    hardening.install(agent, guarded, cw, MissionEpoch, semantic)
    return agent, guarded, cw, semantic, calls


def test_live_refutation_executes_when_live_effective_policy_advertised_it():
    task = {
        "id": "code-refute",
        "project_plan": {"revision": 1},
    }
    state = {
        "schema": "nexus_coding_forced_action.v1",
        "status": "active",
        "state_key": "live-edit",
        "action_kind": "edit",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_refute_hypothesis",
        ],
    }
    agent, guarded, cw, _semantic, calls = install_hardening(task, state)

    result = agent._run_tool(
        "code-refute",
        "coding_refute_hypothesis",
        {
            "reason": "Verified frontend evidence contradicts the durable hypothesis.",
            "contradicting_evidence": "image_catalog_ui.js already renders management.ui_url.",
        },
        git_token_value=None,
    )

    assert result["ok"] is True
    assert result["refuted"] is True
    assert result["refutation_count"] == 1
    assert calls == []
    assert cw.task[MissionEpoch.REFUTATION_KEY]["status"] == "active"
    assert cw.task[MissionEpoch.REFUTATION_KEY]["state_key"] == "live-edit"
    assert guarded._run_tool_with_semantic_acceptance is agent._run_tool


def test_terminal_ready_mission_forces_coding_finish_only():
    task = terminal_ready_task()
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    state = agent.forced_action.active_state(task)

    assert state["schema"] == "nexus_coding_terminal_convergence.v1"
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]
    assert state["terminal_acceptance_pending"] is True
    assert "independent semantic acceptance reviewer" in state["required_action"]
    prompt = agent.forced_action.prompt_context(task)
    assert "terminal-acceptance mode is ACTIVE" in prompt
    assert "Call coding_finish now" in prompt


def test_status_only_plan_update_does_not_invalidate_terminal_readiness():
    task = terminal_ready_task()
    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 30.0
    task["project_plan"]["items"] = [
        {"id": "verify", "title": "Verify", "status": "done", "summary": "complete"}
    ]
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    state = agent.forced_action.active_state(task)

    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]
    assert hardening._readiness_threshold(task, MissionEpoch) == 10.0


def test_generic_progress_note_does_not_invalidate_terminal_readiness():
    task = terminal_ready_task()
    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 30.0
    task["project_plan"]["note"] = "Validation and diff review are complete; ready for acceptance."
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    state = agent.forced_action.active_state(task)

    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]
    assert hardening._readiness_threshold(task, MissionEpoch) == 10.0


def test_semantic_rejection_reopens_execution_until_acceptance_state_changes():
    task = terminal_ready_task()
    task["agent_events"].append(
        {
            "type": "semantic_acceptance_review",
            "ts": 22.0,
            "accepted": False,
            "reason": "Patch leaves the causal early-return path untouched.",
        }
    )
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    assert agent.forced_action.active_state(task) == {}
    assert agent.forced_action.prompt_context(task) == "BASE POLICY PROMPT"


def test_replacement_hypothesis_after_rejection_must_be_revalidated_and_rereviewed():
    task = terminal_ready_task()
    task["agent_events"].append(
        {
            "type": "semantic_acceptance_review",
            "ts": 22.0,
            "accepted": False,
            "reason": "Unsupported deployment-state assumption.",
        }
    )
    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 23.0
    task["project_plan"]["note"] = HYPOTHESIS_B
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    assert hardening._readiness_threshold(task, MissionEpoch) == 23.0
    assert agent.forced_action.active_state(task) == {}

    task["coding_validation_provenance"]["ts"] = 24.0
    task["agent_events"].append(
        {
            "type": "tool_finished",
            "name": "coding_git_diff",
            "ts": 25.0,
            "result": {"ok": True},
        }
    )
    state = agent.forced_action.active_state(task)
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]


def test_semantic_reviewer_is_warned_against_unverified_runtime_configuration_claims():
    task = terminal_ready_task()
    _agent, _guarded, _cw, semantic, _calls = install_hardening(task)

    system, user = semantic.build_review_messages(
        original_request="restore link",
        current_request="restore link",
        hypothesis="Root cause: env vars are unset",
        diff_text="+ fallback",
    )

    assert user == "BASE REVIEW USER"
    assert "runtime, deployment, environment-variable, or configuration" in system
    assert "is not evidence that the variable is unset" in system
