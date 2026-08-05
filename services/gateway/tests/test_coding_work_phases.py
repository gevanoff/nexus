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


def test_review_phase_moves_from_discovery_to_report_decision():
    task = _review_task()
    assert phases.advance_phase(task, stage="observe")["phase"] == phases.DISCOVERY
    decision = phases.advance_phase(task, stage="interrupt")
    assert decision["phase"] == phases.DECISION
    assert decision["decision"] == "report_only"


def test_fix_phase_moves_from_discovery_to_execution():
    task = _review_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True
    decision = phases.advance_phase(task, stage="interrupt")
    assert decision["phase"] == phases.EXECUTION
    assert decision["decision"] == "remediate"


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

