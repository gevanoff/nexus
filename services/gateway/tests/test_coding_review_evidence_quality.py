from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_work_phases as phases


def _report_task(*, phase: str = phases.DISCOVERY, stored_evidence: str = "stored-evidence") -> dict:
    return {
        "mission": {
            "goal": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
            "completion_policy": {"require_file_changes": False},
        },
        "agent_run_id": "run-2",
        "agent_events": [{"type": "started", "run_id": "run-2"}],
        "agent_work_phase": phases.phase_state(
            {},
            phase=phase,
            decision="report_only" if phase == phases.DECISION else "",
        ),
        "agent_progress_state": {
            "observation": {"evidence_fingerprint": stored_evidence},
        },
    }


def test_report_discovery_prompt_requires_authoritative_scope_and_evidence_labels():
    rendered = phases.phase_prompt(_report_task())

    assert "git rev-parse --is-shallow-repository" in rendered
    assert "fetch enough history" in rendered
    assert "base-to-head or parent-to-head changed-file list" in rendered
    assert "repository-wide search for semantically equivalent coverage" in rendered
    assert "hypotheses, not confirmed defects" in rendered
    assert "working-memory summaries are leads, not evidence" in rendered


def test_report_decision_prompt_downgrades_unverified_findings_before_finish():
    rendered = phases.phase_prompt(_report_task(phase=phases.DECISION))

    assert "Before coding_finish" in rendered
    assert "label every reported item" in rendered
    assert "supporting command, trace, source proof" in rendered
    assert "downgrade the finding" in rendered
    assert "state the one missing check" in rendered


def test_fix_mission_does_not_receive_report_only_scope_guidance():
    task = _report_task()
    task["mission"]["completion_policy"]["require_file_changes"] = True

    rendered = phases.phase_prompt(task)

    assert "git rev-parse --is-shallow-repository" not in rendered
    assert "repository-wide search for semantically equivalent coverage" not in rendered


def test_mismatched_tool_call_ids_do_not_pair_validation_evidence():
    task = _report_task()
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "name": "coding_run_command",
                "tool_call_id": "validation-start",
                "args": {"argv": ["python", "-m", "pytest", "tests/test_phase.py"]},
            },
            {
                "type": "tool_finished",
                "name": "coding_run_command",
                "tool_call_id": "different-finish",
                "result": {"ok": False, "returncode": 1, "stderr": "FAILED test_phase.py::test_bad"},
            },
        ]
    )

    assert phases.discovery_evidence_fingerprint(task) == "stored-evidence"


def test_validation_events_without_call_ids_pair_in_fifo_order():
    task = _report_task()
    task["agent_events"].extend(
        [
            {
                "type": "tool_started",
                "name": "coding_run_command",
                "args": {"argv": ["python", "-m", "pytest", "tests/test_phase.py"]},
            },
            {
                "type": "tool_finished",
                "name": "coding_run_command",
                "result": {"ok": False, "returncode": 1, "stderr": "FAILED test_phase.py::test_bad"},
            },
        ]
    )

    fingerprint = phases.discovery_evidence_fingerprint(task)

    assert fingerprint
    assert fingerprint != "stored-evidence"


def test_empty_run_id_uses_only_the_latest_started_segment():
    task = _report_task()
    task["agent_run_id"] = ""
    task["agent_events"] = [
        {"type": "started", "run_id": "old-run"},
        {
            "type": "tool_started",
            "name": "coding_run_command",
            "tool_call_id": "old-validation",
            "args": {"argv": ["pytest", "tests/test_old.py"]},
        },
        {
            "type": "tool_finished",
            "name": "coding_run_command",
            "tool_call_id": "old-validation",
            "result": {"ok": False, "returncode": 1, "stderr": "FAILED tests/test_old.py::test_old"},
        },
        {"type": "started", "run_id": ""},
    ]

    assert phases.discovery_evidence_fingerprint(task) == "stored-evidence"


def test_missing_started_event_does_not_scan_unbounded_history():
    task = _report_task()
    task["agent_run_id"] = ""
    task["agent_events"] = [
        {
            "type": "tool_started",
            "name": "coding_run_command",
            "args": {"argv": ["pytest", "tests/test_old.py"]},
        },
        {
            "type": "tool_finished",
            "name": "coding_run_command",
            "result": {"ok": False, "returncode": 1, "stderr": "FAILED tests/test_old.py::test_old"},
        },
    ]

    assert phases.discovery_evidence_fingerprint(task) == "stored-evidence"


def test_validation_command_forms_cover_supported_runners_without_installs():
    assert phases._is_validation_command(["pytest", "tests/test_api.py"])
    assert phases._is_validation_command(["ruff", "check", "."])
    assert phases._is_validation_command(["mypy", "app"])
    assert phases._is_validation_command(["python", "-m", "unittest", "tests.test_api"])
    assert phases._is_validation_command(["python3", "test_smoke.py"])
    assert phases._is_validation_command(["node", "--check", "app.js"])
    assert phases._is_validation_command(["node", "--test"])
    assert phases._is_validation_command(["npm", "run", "test:unit"])
    assert phases._is_validation_command(["pnpm", "lint"])
    assert phases._is_validation_command(["yarn", "build"])
    assert phases._is_validation_command(["uv", "run", "pytest", "tests/test_api.py"])
    assert phases._is_validation_command(["uv", "run", "git", "diff", "--check"])
    assert phases._is_validation_command(["git", "diff", "--cached", "--check"])

    assert not phases._is_validation_command(["node", "app.js"])
    assert not phases._is_validation_command(["npm", "install", "lint-staged"])
    assert not phases._is_validation_command(["yarn", "add", "check-deps"])
    assert not phases._is_validation_command(["uv", "add", "ruff"])
    assert not phases._is_validation_command(["git", "status"])


def test_uv_run_options_with_values_preserve_the_nested_validation_command():
    assert phases._is_validation_command(
        ["uv", "run", "--project", "services/gateway", "pytest", "tests/test_api.py"]
    )
    assert phases._is_validation_command(
        ["uv", "run", "--project=services/gateway", "pytest", "tests/test_api.py"]
    )
    assert phases._is_validation_command(
        ["uv", "run", "--directory", "services/gateway", "git", "diff", "--check"]
    )
    assert phases._is_validation_command(
        ["uv", "run", "--with", "ruff", "ruff", "check", "."]
    )
    assert phases._is_validation_command(
        ["uv", "run", "-p", "3.11", "python", "-m", "pytest", "tests/test_api.py"]
    )
    assert phases._is_validation_command(
        ["uv", "run", "-p3.11", "python", "-m", "pytest", "tests/test_api.py"]
    )


def test_uv_run_flag_options_and_separator_preserve_the_nested_command():
    assert phases._is_validation_command(
        ["uv", "run", "--no-sync", "--offline", "--", "git", "diff", "--cached", "--check"]
    )


def test_uv_run_option_values_cannot_be_mistaken_for_validation_commands():
    assert not phases._is_validation_command(
        ["uv", "run", "--project", "pytest", "python", "app.py"]
    )
    assert not phases._is_validation_command(["uv", "run", "--project"])
    assert not phases._is_validation_command(["uv", "run", "--with", "ruff", "python", "app.py"])
