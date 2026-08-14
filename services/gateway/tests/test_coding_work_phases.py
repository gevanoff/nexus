from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_agent
from app import coding_runtime_guardrails as guardrails
from app import coding_stagnation_resilience as resilience
from app import coding_work_phases as phases


def _review_task() -> dict:
    return {
        "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
        "mission": {"completion_policy": {"require_file_changes": False}},
        "agent_run_id": "run-1",
        "agent_events": [{"type": "started", "run_id": "run-1"}],
    }


def _source_result(call_id: str, name: str, path: str, *, query: str = "") -> list[dict]:
    args = {"path": path}
    if query:
        args["query"] = query
    return [
        {
            "type": "tool_started",
            "tool_call_id": call_id,
            "name": name,
            "args": args,
        },
        {
            "type": "tool_finished",
            "tool_call_id": call_id,
            "name": name,
            "result": {"ok": True, "content": "evidence"},
        },
    ]


def test_review_phase_moves_from_discovery_to_report_decision():
    task = _review_task()
    assert phases.advance_phase(task, stage="observe")["phase"] == phases.DISCOVERY
    decision = phases.advance_phase(task, stage="interrupt")
    assert decision["phase"] == phases.DECISION
    assert decision["decision"] == "report_only"


def test_fix_phase_moves_from_discovery_to_evidence_decision_not_execution():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    decision = phases.advance_phase(task, stage="interrupt")
    assert decision["phase"] == phases.DECISION
    assert decision["decision"] == "evaluate_remediation"
    assert "evidence gate" in decision["reason"]


def test_fix_decision_stays_locked_without_causal_repository_evidence():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    task["agent_work_phase"] = phases.phase_state(
        task,
        phase=phases.DECISION,
        decision="evaluate_remediation",
    )
    task["agent_events"].extend(
        _source_result("read-html", "coding_read_file", "services/gateway/app/static/image.html")
    )

    decision = phases.advance_phase(task, stage="interrupt")

    assert decision["phase"] == phases.DECISION
    assert decision["decision"] == "evidence_required"


def test_fix_decision_unlocks_after_two_distinct_successful_source_targets():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    task["agent_work_phase"] = phases.phase_state(
        task,
        phase=phases.DECISION,
        decision="evaluate_remediation",
    )
    task["agent_events"].extend(
        _source_result("read-html", "coding_read_file", "services/gateway/app/static/image.html")
    )
    task["agent_events"].extend(
        _source_result(
            "search-management",
            "coding_search_text",
            "services/gateway/app/static",
            query="model_management ui_url",
        )
    )

    decision = phases.advance_phase(task, stage="interrupt")

    assert phases.decision_evidence_ready(task) is True
    assert decision["phase"] == phases.EXECUTION
    assert decision["decision"] == "remediate"


def test_failed_or_rejected_inspection_does_not_unlock_execution():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    task["agent_work_phase"] = phases.phase_state(task, phase=phases.DECISION)
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "tool_call_id": "read-1",
                "name": "coding_read_file",
                "args": {"path": "a.py"},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "read-1",
                "name": "coding_read_file",
                "result": {"ok": False, "error": "read failed"},
            },
            {
                "type": "tool_started",
                "tool_call_id": "read-2",
                "name": "coding_read_file",
                "args": {"path": "b.py"},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "read-2",
                "name": "coding_read_file",
                "result": {"ok": False, "error": "forced_action_tool_rejected"},
            },
        ]
    )

    assert phases.successful_source_evidence_targets(task["agent_events"]) == set()
    assert phases.decision_evidence_ready(task) is False
    assert phases.advance_phase(task, stage="recovery")["phase"] == phases.DECISION


def test_validation_evidence_can_unlock_remediation_without_source_counting():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    task["agent_work_phase"] = phases.phase_state(task, phase=phases.DECISION)
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "tool_call_id": "validation-1",
                "name": "coding_run_command",
                "args": {"argv": ["python", "-m", "pytest", "tests/test_api.py"]},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "validation-1",
                "name": "coding_run_command",
                "result": {"ok": False, "returncode": 1, "stderr": "FAILED test_api.py::test_auth"},
            },
        ]
    )

    assert phases.decision_evidence_ready(task) is True
    assert phases.advance_phase(task, stage="interrupt")["phase"] == phases.EXECUTION


def test_failed_validation_is_discovery_evidence():
    task = _review_task()
    before = phases.discovery_evidence_fingerprint(task)
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "tool_call_id": "validation-1",
                "name": "coding_run_command",
                "args": {"argv": ["python", "-m", "pytest", "tests/test_api.py"]},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "validation-1",
                "name": "coding_run_command",
                "result": {"ok": False, "returncode": 1, "stderr": "FAILED test_api.py::test_auth"},
            },
        ]
    )
    after = phases.discovery_evidence_fingerprint(task)
    assert after
    assert after != before


def test_new_inspection_targets_do_not_reset_discovery_progress():
    task = _review_task()
    before = phases.discovery_evidence_fingerprint(task)
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "tool_call_id": "read-1",
                "name": "coding_read_file_lines",
                "args": {"path": "services/gateway/app/auth.py", "start_line": 1, "line_count": 200},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "read-1",
                "name": "coding_read_file_lines",
                "result": {"ok": True, "content": "new source text"},
            },
            {
                "type": "tool_started",
                "tool_call_id": "search-1",
                "name": "coding_run_command",
                "args": {"argv": ["rg", "Client IP not allowed", "services/gateway"]},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "search-1",
                "name": "coding_run_command",
                "result": {"ok": True, "stdout": "services/gateway/app/auth.py:42"},
            },
        ]
    )
    assert phases.discovery_evidence_fingerprint(task) == before


def test_discovery_evidence_resets_progress_but_execution_reads_do_not():
    previous = guardrails.ProgressState(
        observation=guardrails.ProgressObservation(
            cycle=1,
            workspace_fingerprint="same",
            plan_revision=0,
            validation_revision=0,
            diff_review_revision=0,
            finish_state="running",
            guidance_revision=0,
            evidence_fingerprint="old",
            work_phase="discovery",
        ),
        stagnant_cycles=4,
    )
    discovery = guardrails.ProgressObservation(
        cycle=2,
        workspace_fingerprint="same",
        plan_revision=0,
        validation_revision=0,
        diff_review_revision=0,
        finish_state="running",
        guidance_revision=0,
        evidence_fingerprint="new",
        work_phase="discovery",
    )
    decision = guardrails.evaluate_cycle_progress(previous, discovery, max_stagnant_cycles=8)
    assert decision.progressed is True
    assert decision.state.stagnant_cycles == 0

    execution_previous = guardrails.ProgressState(
        observation=guardrails.ProgressObservation(
            cycle=1,
            workspace_fingerprint="same",
            plan_revision=0,
            validation_revision=0,
            diff_review_revision=0,
            finish_state="running",
            guidance_revision=0,
            evidence_fingerprint="old",
            work_phase="execution",
        ),
        stagnant_cycles=2,
    )
    execution = guardrails.ProgressObservation(
        cycle=2,
        workspace_fingerprint="same",
        plan_revision=0,
        validation_revision=0,
        diff_review_revision=0,
        finish_state="running",
        guidance_revision=0,
        evidence_fingerprint="new",
        work_phase="execution",
    )
    execution_decision = guardrails.evaluate_cycle_progress(execution_previous, execution, max_stagnant_cycles=8)
    assert execution_decision.progressed is False
    assert execution_decision.state.stagnant_cycles == 3


def test_review_mission_policy_overrides_fix_word_in_continuation_prompt():
    task = _review_task()
    task["agent_run_prompt"] = "Fix them."
    assert coding_agent._request_expects_workspace_edits(task) is True
    assert coding_agent._mission_requires_workspace_edits(task) is False


def test_review_decision_working_memory_finishes_without_forcing_edits():
    task = _review_task()
    working = resilience.build_working_memory(
        task,
        state_key="state",
        controller={"classification": "validation_loop", "stage": "interrupt", "work_phase": "decision", "phase_decision": "report_only"},
        ledger=[],
        events=[],
    )
    assert working["work_phase"] == "decision"
    assert working["next_action_kind"] == "finish"
    assert working["next_action"].startswith("Call coding_finish")


def test_fix_decision_working_memory_requests_evidence_not_edit():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    working = resilience.build_working_memory(
        task,
        state_key="state",
        controller={
            "classification": "stagnant_execution",
            "stage": "interrupt",
            "work_phase": "decision",
            "phase_decision": "evidence_required",
        },
        ledger=[],
        events=[],
    )
    assert working["work_phase"] == "decision"
    assert working["next_action_kind"] != "edit"
    assert "evidence" in working["next_action"].lower()


def test_validation_evidence_changes_durable_state_key_but_phase_does_not():
    task = _review_task()
    task["agent_progress_state"] = {
        "observation": {
            "workspace_fingerprint": "same",
            "validation_revision": 0,
            "diff_review_revision": 0,
            "finish_state": "running",
            "evidence_fingerprint": "validation-a",
            "work_phase": "discovery",
        }
    }
    initial = resilience.durable_state_key(task)

    task["agent_progress_state"]["observation"]["evidence_fingerprint"] = "validation-b"
    after_evidence = resilience.durable_state_key(task)
    assert after_evidence != initial

    task["agent_progress_state"]["observation"]["work_phase"] = "decision"
    assert resilience.durable_state_key(task) == after_evidence


def test_phase_transition_does_not_mint_guardrail_progress():
    previous = guardrails.ProgressState(
        observation=guardrails.ProgressObservation(
            cycle=5,
            workspace_fingerprint="same",
            plan_revision=0,
            validation_revision=1,
            diff_review_revision=0,
            finish_state="running",
            guidance_revision=0,
            evidence_fingerprint="validation-a",
            work_phase="discovery",
        ),
        stagnant_cycles=4,
    )
    current = guardrails.ProgressObservation(
        cycle=6,
        workspace_fingerprint="same",
        plan_revision=0,
        validation_revision=1,
        diff_review_revision=0,
        finish_state="running",
        guidance_revision=0,
        evidence_fingerprint="validation-a",
        work_phase="decision",
    )

    decision = guardrails.evaluate_cycle_progress(previous, current, max_stagnant_cycles=8)

    assert decision.progressed is False
    assert decision.state.stagnant_cycles == 5


def test_legacy_review_task_derives_report_only_policy():
    task = {
        "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
        "agent_run_id": "run-legacy",
        "agent_events": [{"type": "started", "run_id": "run-legacy"}],
    }

    assert phases.mission_requires_file_changes(task) is False
    decision = phases.advance_phase(task, stage="interrupt")
    assert decision["phase"] == phases.DECISION
    assert decision["decision"] == "report_only"


def test_package_manager_installs_are_not_validation_evidence():
    assert phases._is_validation_command(["npm", "install", "lint-staged"]) is False
    assert phases._is_validation_command(["yarn", "add", "check-deps"]) is False
    assert phases._is_validation_command(["npm", "run", "test:unit"]) is True
    assert phases._is_validation_command(["pnpm", "lint"]) is True


def test_empty_started_event_does_not_hide_current_run_validation():
    task = _review_task()
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "tool_call_id": "validation-current",
                "name": "coding_run_command",
                "args": {"argv": ["python", "-m", "pytest", "tests/test_current.py"]},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "validation-current",
                "name": "coding_run_command",
                "result": {"ok": False, "returncode": 1, "stderr": "current failure"},
            },
            {"type": "started", "run_id": ""},
        ]
    )

    assert phases.discovery_evidence_fingerprint(task)


def test_report_only_mission_prompt_does_not_advertise_fix_expectation():
    task = _review_task()
    task.update(
        {
            "id": "code_phase_prompt",
            "base_branch": "main",
            "branch_name": "review",
            "agent_run_prompt": "Fix them.",
            "project_plan": {"items": []},
        }
    )

    rendered = coding_agent._system_prompt(task)

    assert "This request is fix-oriented" not in rendered


def test_decision_prompt_requires_causal_hypothesis_and_competing_explanation():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    task["agent_work_phase"] = phases.phase_state(task, phase=phases.DECISION)

    rendered = phases.phase_prompt(task)

    assert "Do not edit yet" in rendered
    assert "causal mechanism" in rendered
    assert "competing explanation" in rendered
    assert "observable result" in rendered


def test_execution_prompt_demands_adversarial_diff_review():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    task["agent_work_phase"] = phases.phase_state(task, phase=phases.EXECUTION)

    rendered = phases.phase_prompt(task)

    assert "adversarial diff review" in rendered
    assert "duplicates or bypasses an existing mechanism" in rendered
    assert "hardcodes environment-specific values" in rendered


def test_validation_fingerprint_survives_phase_transition():
    task = _review_task()
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "tool_call_id": "validation-stable",
                "name": "coding_run_command",
                "args": {"argv": ["python", "-m", "pytest", "tests/test_stable.py"]},
            },
            {
                "type": "tool_finished",
                "tool_call_id": "validation-stable",
                "name": "coding_run_command",
                "result": {"ok": False, "returncode": 1, "stderr": "stable failure"},
            },
        ]
    )
    discovery = phases.discovery_evidence_fingerprint(task)
    task["agent_work_phase"] = phases.phase_state(task, phase=phases.DECISION, decision="report_only")

    assert phases.discovery_evidence_fingerprint(task) == discovery


def test_legacy_review_audit_uses_phase_mission_fallback():
    task = {
        "prompt": "Review this workspace for bugs and missing tests.",
        "agent_run_prompt": "Fix them.",
    }

    assert coding_agent._mission_requires_workspace_edits(task) is False
