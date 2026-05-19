from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import pytest

from .. import coding_workspace as cw


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
        "agent_cycle": 5,
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
    task = _base_task(agent_status="running", agent_events=[{"ts": 1000, "type": "cycle_started", "cycle": 1}])

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


def test_set_task_coding_model_updates_stopped_workspace(monkeypatch, tmp_path):
        task = _base_task(coding_model="local_vllm")

        monkeypatch.setattr(cw, "coding_enabled", lambda: True)
        monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path)

        cw.save_task(task)
        updated = cw.set_task_coding_model(task["id"], coding_model="coder")

        assert updated["coding_model"] == "coder"
        stored = cw.load_task(task["id"])
        assert stored["coding_model"] == "coder"
        assert stored["agent_events"][-1]["type"] == "model_updated"


def test_set_task_coding_model_rejects_active_workspace(monkeypatch, tmp_path):
        task = _base_task(agent_status="running", coding_model="local_vllm")

        monkeypatch.setattr(cw, "coding_enabled", lambda: True)
        monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path)

        cw.save_task(task)

        with pytest.raises(Exception) as exc_info:
            cw.set_task_coding_model(task["id"], coding_model="coder")

        assert getattr(exc_info.value, "status_code", None) == 409
