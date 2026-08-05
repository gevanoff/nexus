from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_runtime_guardrails as guardrails
from app import coding_work_phases as phases


def _validation_events(run_id: str, *, stderr: str) -> list[dict]:
    return [
        {"type": "started", "run_id": run_id},
        {
            "type": "tool_started",
            "tool_call_id": f"validation-{run_id}",
            "name": "coding_run_command",
            "args": {"argv": ["python", "-m", "pytest", "tests/test_phase.py"]},
        },
        {
            "type": "tool_finished",
            "tool_call_id": f"validation-{run_id}",
            "name": "coding_run_command",
            "result": {"ok": False, "returncode": 1, "stderr": stderr},
        },
    ]


def test_resumed_run_reuses_stored_discovery_evidence_until_new_validation():
    task = {
        "agent_run_id": "run-2",
        "agent_events": [
            *_validation_events("run-1", stderr="FAILED tests/test_phase.py::test_old"),
            {"type": "started", "run_id": "run-2"},
        ],
        "agent_progress_state": {
            "observation": {
                "evidence_fingerprint": "persisted-validation-evidence",
            }
        },
    }

    fingerprint = phases.discovery_evidence_fingerprint(task)

    assert fingerprint == "persisted-validation-evidence"

    previous = guardrails.ProgressState(
        observation=guardrails.ProgressObservation(
            cycle=7,
            workspace_fingerprint="same",
            plan_revision=0,
            validation_revision=0,
            diff_review_revision=0,
            finish_state="running",
            guidance_revision=0,
            evidence_fingerprint=fingerprint,
            work_phase="discovery",
        ),
        stagnant_cycles=4,
    )
    current = guardrails.ProgressObservation(
        cycle=1,
        workspace_fingerprint="same",
        plan_revision=0,
        validation_revision=0,
        diff_review_revision=0,
        finish_state="running",
        guidance_revision=0,
        evidence_fingerprint=fingerprint,
        work_phase="discovery",
    )

    assert guardrails.evaluate_cycle_progress(previous, current, max_stagnant_cycles=8).progressed is False


def test_current_run_validation_supersedes_stored_discovery_evidence():
    task = {
        "agent_run_id": "run-2",
        "agent_events": _validation_events(
            "run-2",
            stderr="FAILED tests/test_phase.py::test_new",
        ),
        "agent_progress_state": {
            "observation": {
                "evidence_fingerprint": "persisted-validation-evidence",
            }
        },
    }

    fingerprint = phases.discovery_evidence_fingerprint(task)

    assert fingerprint
    assert fingerprint != "persisted-validation-evidence"


def test_equivalent_validation_output_ignores_volatile_details_and_ordering():
    first = {
        "agent_run_id": "run-1",
        "agent_events": _validation_events(
            "run-1",
            stderr=(
                "\x1b[31m2026-08-05T04:39:19Z FAILED tests/test_phase.py::test_same\x1b[0m\n"
                "0.22s call tests/test_phase.py::test_same\n"
                "/tmp/pytest-of-user/pytest-4/test_same0/output.txt\n"
                "tests/test_phase.py::test_other\n"
                "2 failed in 6.86s\n"
            ),
        ),
    }
    second = {
        "agent_run_id": "run-2",
        "agent_events": _validation_events(
            "run-2",
            stderr=(
                "tests/test_phase.py::test_other\n"
                "/tmp/pytest-of-user/pytest-99/test_same0/output.txt\n"
                "2026-08-05T05:41:03+00:00 FAILED tests/test_phase.py::test_same\n"
                "9.91s call tests/test_phase.py::test_same\n"
                "2 failed in 13.24s\n"
            ),
        ),
    }

    assert phases.discovery_evidence_fingerprint(first) == phases.discovery_evidence_fingerprint(second)


def test_meaningful_validation_failure_change_updates_fingerprint():
    first = {
        "agent_run_id": "run-1",
        "agent_events": _validation_events(
            "run-1",
            stderr="FAILED tests/test_phase.py::test_first\n1 failed in 1.00s\n",
        ),
    }
    second = {
        "agent_run_id": "run-2",
        "agent_events": _validation_events(
            "run-2",
            stderr="FAILED tests/test_phase.py::test_second\n1 failed in 9.00s\n",
        ),
    }

    assert phases.discovery_evidence_fingerprint(first) != phases.discovery_evidence_fingerprint(second)
