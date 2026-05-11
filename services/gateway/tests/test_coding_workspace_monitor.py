from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from pathlib import Path

from app import coding_workspace as cw


def _base_task(**overrides):
    task = {
        "schema": cw.SCHEMA,
        "id": "code_abcdef123456",
        "status": "ready",
        "created_at": 0,
        "updated_at": 1000,
        "owner": "test",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_abcdef123456",
        "prompt": "Fix the failing coding task.",
        "workspace_path": "/tmp/code_abcdef123456",
        "repo_path": "/tmp/code_abcdef123456/repo",
        "agent_status": "stopped",
        "agent_turn": 5,
        "agent_max_turns": 1000,
        "agent_last_event_at": 1000,
        "agent_events": [
            {"ts": 990, "type": "no_tool_call", "summary": "tool call missing"},
            {"ts": 995, "type": "no_tool_call", "summary": "tool call missing"},
            {"ts": 1000, "type": "no_tool_call", "summary": "tool call missing"},
        ],
    }
    task.update(overrides)
    return task


def test_task_monitor_summary_flags_stopped_and_repeated_no_tool_calls(monkeypatch):
    task = _base_task()

    monkeypatch.setattr(cw, "git_change_summary", lambda task_id: {"ok": True, "counts": {"total": 0}, "files": []})
    monkeypatch.setattr(cw, "_repo_path", lambda task: Path("/tmp/code_abcdef123456/repo"))
    monkeypatch.setattr(
        cw,
        "_git_base_branch_diff",
        lambda repo, *, base_branch: {
            "changes": {"counts": {"total": 1}, "files": [{"path": "README.md", "status": "M", "kind": "modified"}]},
            "committed_changes": {"counts": {"total": 1}, "files": [{"path": "README.md", "status": "M", "kind": "modified"}]},
            "error": "",
        },
    )

    summary = cw._task_monitor_summary(task, now=2000, stalled_after_sec=300)

    assert summary["needs_attention"] is True
    assert "run_stopped" in summary["attention"]
    assert "repeated_no_tool_call" in summary["attention"]
    assert "resume" in summary["safe_actions"]
    assert "guide_and_resume" in summary["safe_actions"]
    assert summary["workspace_changes"]["counts"]["total"] == 1


def test_task_monitor_summary_flags_running_stall(monkeypatch):
    task = _base_task(agent_status="running", agent_events=[{"ts": 1000, "type": "turn_started", "turn": 1}])

    monkeypatch.setattr(cw, "git_change_summary", lambda task_id: {"ok": True, "counts": {"total": 0}, "files": []})
    monkeypatch.setattr(cw, "_repo_path", lambda task: Path("/tmp/code_abcdef123456/repo"))
    monkeypatch.setattr(
        cw,
        "_git_base_branch_diff",
        lambda repo, *, base_branch: {
            "changes": {"counts": {"total": 0}, "files": []},
            "committed_changes": {"counts": {"total": 0}, "files": []},
            "error": "",
        },
    )

    summary = cw._task_monitor_summary(task, now=2000, stalled_after_sec=300)

    assert summary["needs_attention"] is True
    assert "running_stalled" in summary["attention"]
    assert summary["recommended_action"] == "guidance"


def test_monitor_tasks_can_filter_attention(monkeypatch):
    task = _base_task()

    monkeypatch.setattr(cw, "list_tasks", lambda limit=20: [{"id": task["id"]}])
    monkeypatch.setattr(cw, "load_task", lambda task_id: task)
    monkeypatch.setattr(cw, "git_change_summary", lambda task_id: {"ok": True, "counts": {"total": 0}, "files": []})
    monkeypatch.setattr(cw, "_repo_path", lambda task: Path("/tmp/code_abcdef123456/repo"))
    monkeypatch.setattr(
        cw,
        "_git_base_branch_diff",
        lambda repo, *, base_branch: {
            "changes": {"counts": {"total": 0}, "files": []},
            "committed_changes": {"counts": {"total": 0}, "files": []},
            "error": "",
        },
    )

    payload = cw.monitor_tasks(limit=5, only_attention=True, stalled_after_sec=300)

    assert payload["ok"] is True
    assert payload["counts"]["attention"] == 1
    assert len(payload["tasks"]) == 1