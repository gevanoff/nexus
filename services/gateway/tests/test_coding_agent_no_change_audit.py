from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_agent as ca


def test_no_change_audit_fails_finish_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="Completed requested work.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is False
    assert "called coding_finish" in summary
    assert event is not None
    assert event["type"] == "no_change_audit"


def test_no_change_audit_fails_turn_limit_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=False,
        finish_success=False,
        finish_summary="Turn limit reached before the agent called coding_finish.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is False
    assert "No-change audit" in summary
    assert event is not None
    assert event["type"] == "no_change_audit"


def test_no_change_audit_preserves_runs_with_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="Completed requested work.",
        committed_changes=False,
        uncommitted_changes=True,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is True
    assert summary == "Completed requested work."
    assert event is None


def test_fix_oriented_request_is_marked_edit_expected():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Debug this failing workflow and fix the root cause in the repo.",
    }

    assert ca._request_expects_workspace_edits(task) is True
    prompt = ca._system_prompt(task)
    assert "This request is fix-oriented." in prompt
    assert "Do not stop at diagnosis alone" in prompt


def test_review_request_does_not_get_fix_oriented_prompt():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
    }

    assert ca._request_expects_workspace_edits(task) is False
    prompt = ca._system_prompt(task)
    assert "This request is fix-oriented." not in prompt


def test_max_turns_allows_up_to_ten_thousand():
    assert ca._max_turns() == 1000
    assert ca._max_turns(5000) == 5000
    assert ca._max_turns(20000) == 10000