from __future__ import annotations

import hashlib
from types import SimpleNamespace

from app import coding_acceptance_convergence_hardening as hardening
from app import coding_completion_state_hardening as completion
from app import coding_terminal_acceptance_hardening as terminal
from app import coding_work_phases


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
        state = self.active_state(task)
        names = set(state.get("allowed_tools") or [])
        # Match the real mission-acceptance overlay: refutation is an effective
        # allowed tool in edit mode without widening active_state.allowed_tools.
        if str(state.get("action_kind") or "") == "edit":
            names.add("coding_refute_hypothesis")
        return names

    def prompt_context(self, _task):
        return "BASE POLICY PROMPT"


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

    def workspace_progress_fingerprint(self, _task_id):
        return "workspace-after-edit"


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
        "agent_run_id": "run-terminal",
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
            "schema": "nexus_coding_hypothesis_lifecycle.v1",
            "status": "consumed",
            "consumed_at": 10.0,
            "plan_revision": 2,
            "note_fingerprint": hashlib.sha256(HYPOTHESIS_A.encode("utf-8")).hexdigest(),
            "verified_evidence_digest": "Verified repository evidence: app.py:10-20",
        },
        "coding_validation_provenance": {
            "schema": "nexus_coding_validation_provenance.v1",
            "argv": ["python3", "-m", "py_compile", "app.py"],
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
        "commands": [],
    }


def install_hardening(task, state=None):
    policy = Policy(state)
    calls = []
    cw = CW(task)
    events = []

    def mutate(_task_id, updates):
        cw.task.update(dict(updates))

    def append(_task_id, event):
        recorded = dict(event)
        events.append(recorded)
        cw.task.setdefault("agent_events", []).append(recorded)

    agent = SimpleNamespace(
        forced_action=policy,
        events=events,
        _mutate_task=mutate,
        _append_event=append,
        _run_tool=lambda task_id, name, args, *, git_token_value: (
            calls.append((task_id, name, args, git_token_value))
            or {"ok": False, "error": "stale_handler_called"}
        ),
    )
    guarded = SimpleNamespace(_run_tool_with_semantic_acceptance=agent._run_tool)
    semantic = SemanticAcceptance()
    hardening.install(agent, guarded, cw, MissionEpoch, semantic)
    return agent, guarded, cw, semantic, calls


def add_validation_events(task, *, call_id, started_at, argv, ok, error="", stdout="", stderr=""):
    task.setdefault("agent_events", []).extend(
        [
            {
                "type": "tool_started",
                "name": "coding_run_command",
                "tool_call_id": call_id,
                "ts": started_at,
                "args": {"argv": list(argv), "cwd": ""},
            },
            {
                "type": "tool_finished",
                "name": "coding_run_command",
                "tool_call_id": call_id,
                "ts": started_at + 0.1,
                "result": {
                    "ok": ok,
                    "error": error,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            },
        ]
    )


def test_live_refutation_uses_canonical_allowed_names_not_state_allowed_tools():
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
        ],
    }
    agent, guarded, cw, _semantic, calls = install_hardening(task, state)

    assert "coding_refute_hypothesis" not in state["allowed_tools"]
    assert "coding_refute_hypothesis" in agent.forced_action.allowed_tool_names(task)
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


def test_whitespace_only_note_update_matches_legacy_lifecycle_fingerprint():
    task = terminal_ready_task()
    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 30.0
    task["project_plan"]["note"] = f"  {HYPOTHESIS_A}\n"
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    assert hardening._readiness_threshold(task, MissionEpoch) == 10.0
    assert agent.forced_action.active_state(task)["action_kind"] == "finish"


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


def test_trailing_status_section_does_not_change_structured_hypothesis_identity():
    task = terminal_ready_task()
    lifecycle = task["agent_hypothesis_lifecycle"]
    lifecycle["structured_hypothesis_fingerprint"] = (
        hardening._structured_hypothesis_fingerprint_from_note(HYPOTHESIS_A)
    )
    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 30.0
    task["project_plan"]["note"] = HYPOTHESIS_A + "\nStatus: validation and review complete"
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    assert hardening._readiness_threshold(task, MissionEpoch) == 10.0
    assert agent.forced_action.active_state(task)["action_kind"] == "finish"


def test_consumed_evidence_survives_status_only_plan_revision():
    task = terminal_ready_task()
    _agent, _guarded, _cw, _semantic, _calls = install_hardening(task)
    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 30.0

    context = completion._lifecycle_context(task)

    assert "consumed by a repository mutation" in context
    assert "Verified repository evidence: app.py:10-20" in context
    assert "Consumed remediation hypothesis:" in context
    assert HYPOTHESIS_A in context


class _Persistence:
    @staticmethod
    def _verified_evidence_digest(_task, _state):
        return "Verified repository evidence: app.py:10-20"


def test_new_mutation_persists_consumed_hypothesis_for_later_plan_rewrites():
    task = terminal_ready_task()
    task.pop("agent_hypothesis_lifecycle")
    agent, _guarded, cw, _semantic, _calls = install_hardening(task)

    completion._record_consumed_hypothesis(
        agent,
        cw,
        _Persistence,
        task_id=task["id"],
        before_task=task,
        before_state={"causal_evidence_targets": ["app.py"]},
        tool_name="coding_replace_text",
    )
    lifecycle = task["agent_hypothesis_lifecycle"]
    assert lifecycle["consumed_hypothesis_note"] == HYPOTHESIS_A
    assert lifecycle["structured_hypothesis_fingerprint"] == (
        hardening._structured_hypothesis_fingerprint_from_note(HYPOTHESIS_A)
    )

    task["project_plan"] = {
        "revision": 3,
        "updated_at": 30.0,
        "note": "Progress: waiting for semantic acceptance.",
        "items": [],
    }
    context = completion._lifecycle_context(task)
    assert HYPOTHESIS_A in context
    assert "Verified repository evidence: app.py:10-20" in context


def test_unresolved_strong_validation_failure_blocks_terminal_acceptance_across_resume():
    task = terminal_ready_task()
    task["coding_validation_provenance"] = {
        "schema": "nexus_coding_validation_provenance.v1",
        "argv": ["git", "diff", "--check"],
        "ok": True,
        "ts": 18.0,
    }
    add_validation_events(
        task,
        call_id="pytest-fail",
        started_at=14.0,
        argv=["pytest", "-q"],
        ok=False,
        stderr="1 failed",
    )
    add_validation_events(
        task,
        call_id="weak-success",
        started_at=17.0,
        argv=["git", "diff", "--check"],
        ok=True,
    )
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    ready, _ts = hardening._validation_ready(task, 10.0)
    assert ready is False
    assert agent.forced_action.active_state(task) == {}

    add_validation_events(
        task,
        call_id="pytest-pass",
        started_at=19.0,
        argv=["pytest", "-q"],
        ok=True,
    )
    task["coding_validation_provenance"] = {
        "schema": "nexus_coding_validation_provenance.v1",
        "argv": ["pytest", "-q"],
        "ok": True,
        "ts": 19.1,
    }
    ready, _ts = hardening._validation_ready(task, 10.0)
    assert ready is True
    assert agent.forced_action.active_state(task)["action_kind"] == "finish"


def test_validation_history_survives_latest_provenance_overwrite():
    task = terminal_ready_task()
    task["coding_validation_provenance"] = {}
    _agent, _guarded, cw, _semantic, _calls = install_hardening(task)

    terminal._persist_validation_provenance(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["pytest", "-q"],
        cwd="",
        result={"ok": False, "stderr": "1 failed"},
    )
    terminal._persist_validation_provenance(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["git", "diff", "--check"],
        cwd="",
        result={"ok": True, "stderr": ""},
    )

    history = task["coding_validation_provenance"]["history"]
    assert [item["argv"] for item in history[-2:]] == [
        ["pytest", "-q"],
        ["git", "diff", "--check"],
    ]
    assert history[-2]["ok"] is False
    assert history[-1]["ok"] is True
    assert hardening._validation_obligations_ready(task, 10.0) is False


def test_decisive_semantic_rejection_reopens_execution():
    task = terminal_ready_task()
    task["agent_events"].append(
        {
            "type": "semantic_acceptance_state",
            "ts": 22.0,
            "accepted": False,
            "review_error": False,
            "reason": "Patch leaves the causal early-return path untouched.",
        }
    )
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    assert agent.forced_action.active_state(task) == {}
    assert agent.forced_action.prompt_context(task) == "BASE POLICY PROMPT"


def test_retryable_reviewer_failure_does_not_erase_earlier_decisive_rejection():
    task = terminal_ready_task()
    task["agent_events"].extend(
        [
            {
                "type": "semantic_acceptance_state",
                "ts": 22.0,
                "accepted": False,
                "review_error": False,
                "reason": "Patch is causally wrong.",
            },
            {
                "type": "semantic_acceptance_state",
                "ts": 23.0,
                "accepted": False,
                "review_error": True,
                "reason": "review backend unavailable",
            },
        ]
    )
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    rejection = hardening._latest_decisive_rejection(task, 10.0)
    assert rejection["ts"] == 22.0
    assert agent.forced_action.active_state(task) == {}


def test_status_churn_cannot_bypass_semantic_rejection_guard():
    task = terminal_ready_task()
    agent, _guarded, cw, _semantic, calls = install_hardening(task)
    hardening._record_semantic_rejection_guard(
        agent,
        cw,
        MissionEpoch,
        task["id"],
        {
            "error": "semantic_acceptance_rejected",
            "semantic_review": {
                "accepted": False,
                "reason": "Patch is causally wrong.",
                "review_error": False,
            },
        },
    )

    task["project_plan"]["revision"] = 3
    task["project_plan"]["updated_at"] = 30.0
    task["project_plan"]["items"] = [
        {"id": "verify", "title": "Verify", "status": "completed", "summary": "done"}
    ]
    blocked = agent._run_tool(
        task["id"],
        "coding_finish",
        {},
        git_token_value=None,
    )
    assert blocked["error"] == "semantic_acceptance_state_unchanged"
    assert calls == []

    task["project_plan"]["revision"] = 4
    task["project_plan"]["updated_at"] = 31.0
    task["project_plan"]["note"] = HYPOTHESIS_B
    allowed = agent._run_tool(
        task["id"],
        "coding_finish",
        {},
        git_token_value=None,
    )
    assert allowed["error"] == "stale_handler_called"
    assert len(calls) == 1


def test_accepted_review_without_durable_epoch_stays_finish_only_for_retry():
    task = terminal_ready_task()
    task["agent_events"].append(
        {
            "type": "semantic_acceptance_review",
            "ts": 22.0,
            "accepted": True,
            "reason": "Patch is aligned.",
        }
    )
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    state = agent.forced_action.active_state(task)
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]


def test_reviewer_failure_stays_finish_only_instead_of_reopening_coding():
    task = terminal_ready_task()
    task["agent_events"].append(
        {
            "type": "semantic_acceptance_state",
            "ts": 22.0,
            "accepted": False,
            "review_error": True,
            "reason": "semantic reviewer failed upstream",
        }
    )
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)

    state = agent.forced_action.active_state(task)
    assert state["action_kind"] == "finish"
    assert state["allowed_tools"] == ["coding_finish"]


def test_parse_failure_is_recorded_as_retryable_reviewer_error():
    task = terminal_ready_task()
    agent, _guarded, _cw, _semantic, _calls = install_hardening(task)
    fingerprint = "acceptance-fingerprint"

    terminal._record_rejection(
        agent,
        task["id"],
        task,
        fingerprint=fingerprint,
        result={
            "semantic_review": {
                "accepted": False,
                "reason": "semantic reviewer did not return parseable JSON",
                "parse_error": True,
            }
        },
    )

    event = task["agent_events"][-1]
    assert event["type"] == "semantic_acceptance_state"
    assert event["review_error"] is True
    assert terminal._prior_rejection(task, fingerprint) == {}


def test_replacement_hypothesis_after_rejection_must_be_revalidated_and_rereviewed():
    task = terminal_ready_task()
    task["agent_events"].append(
        {
            "type": "semantic_acceptance_state",
            "ts": 22.0,
            "accepted": False,
            "review_error": False,
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
    task["coding_validation_provenance"]["argv"] = ["python3", "-m", "py_compile", "app.py"]
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
