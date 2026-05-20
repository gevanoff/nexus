from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import pytest

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
    assert "resume" not in summary["safe_actions"]
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


def test_validate_command_blocks_dependency_installs():
    with pytest.raises(Exception) as npm_exc:
        cw.validate_command(["npm", "install"])
    with pytest.raises(Exception) as pip_exc:
        cw.validate_command(["python", "-m", "pip", "install", "requests"])
    with pytest.raises(Exception) as uv_exc:
        cw.validate_command(["uv", "add", "requests"])

    assert getattr(npm_exc.value, "status_code", None) == 403
    assert getattr(pip_exc.value, "status_code", None) == 403
    assert getattr(uv_exc.value, "status_code", None) == 403


def test_validate_command_allows_non_mutating_checks():
    assert cw.validate_command(["npm", "run", "typecheck"]) == ["npm", "run", "typecheck"]
    assert cw.validate_command(["uv", "run", "python", "-m", "pytest"]) == ["uv", "run", "python", "-m", "pytest"]


def test_load_task_repairs_unreadable_metadata(monkeypatch, tmp_path):
    task_id = "code_abcdef123456"
    monkeypatch.setattr(cw, "coding_enabled", lambda: True)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path)

    broken = tmp_path / f"{task_id}.json"
    broken.write_text("{bad json", encoding="utf-8")

    repaired = cw.load_task(task_id)

    assert repaired["status"] == "error"
    assert repaired["metadata_error"]["task_file"].endswith(f"{task_id}.json")
    quarantined_path = Path(repaired["metadata_error"]["quarantined_path"])
    assert quarantined_path.exists()

    stored = json.loads(broken.read_text(encoding="utf-8"))
    assert stored["schema"] == cw.SCHEMA
    assert stored["status"] == "error"


def test_archive_task_moves_task_and_workspace_for_forensics(monkeypatch, tmp_path):
    task = _base_task(
        workspace_path=str(tmp_path / "workspaces" / "code_abcdef123456"),
        repo_path=str(tmp_path / "workspaces" / "code_abcdef123456" / "repo"),
    )

    monkeypatch.setattr(cw, "coding_enabled", lambda: True)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")

    workspace = Path(task["workspace_path"])
    repo = Path(task["repo_path"])
    repo.mkdir(parents=True, exist_ok=True)
    repo.joinpath("README.md").write_text("hello\n", encoding="utf-8")

    cw.save_task(task)
    result = cw.archive_task(task["id"], actor="tester", reason="forensics")

    assert result["ok"] is True
    assert Path(result["archived_task"]).exists()
    assert Path(result["manifest"]).exists()
    assert Path(result["archived_workspace"]).exists()
    assert not (tmp_path / "tasks" / f"{task['id']}.json").exists()
    assert not workspace.exists()
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["analysis"]["requested_mode"] == "idle"
    assert manifest["analysis"]["target"] == "local"
    assert manifest["retention"]["preserve"] is False
    assert int(manifest["retention"]["delete_after_ts"] or 0) > int(manifest["archived_at"])
    assert manifest["findings_path"].endswith(".findings.jsonl")


def test_archived_task_settings_list_and_cleanup(monkeypatch, tmp_path):
    task = _base_task(
        workspace_path=str(tmp_path / "workspaces" / "code_abcdef123456"),
        repo_path=str(tmp_path / "workspaces" / "code_abcdef123456" / "repo"),
    )

    monkeypatch.setattr(cw, "coding_enabled", lambda: True)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")

    workspace = Path(task["workspace_path"])
    repo = Path(task["repo_path"])
    repo.mkdir(parents=True, exist_ok=True)
    repo.joinpath("README.md").write_text("hello\n", encoding="utf-8")

    cw.save_task(task)
    archived = cw.archive_task(task["id"], actor="tester", reason="forensics")
    archive_id = archived["archive_id"]

    updated = cw.update_archived_task_settings(
        archive_id,
        preserve=True,
        analysis_mode="manual",
        analysis_target="human",
    )
    assert updated["retention"]["preserve"] is True
    assert updated["analysis"]["requested_mode"] == "manual"
    assert updated["analysis"]["target"] == "human"

    archives = cw.list_archived_tasks(limit=10)
    assert len(archives) == 1
    assert archives[0]["archive_id"] == archive_id
    assert archives[0]["paths"]["workspace"].endswith(archive_id)

    cw.update_archived_task_settings(archive_id, preserve=False, delete_after_ts=1, analysis_target="local", analysis_model="coder")
    cleanup = cw.cleanup_archived_tasks(now=2)
    assert cleanup["count"] == 1
    assert cleanup["purged"][0]["archive_id"] == archive_id
    assert cw.list_archived_tasks(limit=10) == []


def test_task_monitor_summary_flags_metadata_error_and_blocks_resume(monkeypatch):
    task = _base_task(
        status="error",
        agent_status="failed",
        metadata_error={"message": "broken task json"},
        agent_events=[{"ts": 1000, "type": "failed", "summary": "broken task json"}],
    )

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

    assert "metadata_read_failed" in summary["attention"]
    assert "resume" not in summary["safe_actions"]
    assert "guide_and_resume" not in summary["safe_actions"]


def test_task_monitor_summary_flags_finish_gate_for_manual_guidance(monkeypatch):
    task = _base_task(
        agent_status="failed",
        agent_events=[
            {"ts": 995, "type": "finish_gate", "summary": "run validation first"},
            {"ts": 1000, "type": "failed", "summary": "run validation first"},
        ],
    )

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

    assert "finish_gate" in summary["attention"]
    assert "resume" not in summary["safe_actions"]
    assert "guide_and_resume" in summary["safe_actions"]
