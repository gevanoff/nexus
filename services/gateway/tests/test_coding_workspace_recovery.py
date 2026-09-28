from __future__ import annotations

from app import coding_workspace as cw


def _configure_roots(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(cw, "coding_enabled", lambda: True)
    cw._ensure_dirs()


def _task(task_id: str, *, owner: str, branch_name: str, status: str) -> dict:
    run_id = f"run_{task_id}"
    return {
        "schema": cw.SCHEMA,
        "id": task_id,
        "kind": "workspace",
        "status": "ready",
        "created_at": 100.0,
        "updated_at": 100.0,
        "owner": owner,
        "branch_name": branch_name,
        "agent_status": status,
        "agent_run_id": run_id,
        "agent_cycle": 4,
        "agent_auto_resume_pending": status == "interrupted",
        "agent_stop_requested": True,
        "agent_pause_requested": True,
        "agent_summary": "old summary",
        "agent_error": "old error",
        "agent_events": [],
        "agent_runs": [
            {
                "run_id": run_id,
                "status": status,
                "finished_at": None,
                "summary": "old summary",
                "error": "old error",
            }
        ],
    }


def test_recovery_pauses_scheduler_smoke_and_recovers_ordinary_task(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    smoke = _task(
        "code_a1b2c3d4e5f6",
        owner="coding-smoke-scheduler",
        branch_name="nexus-coding-smoke/123-abc123",
        status="running",
    )
    ordinary = _task(
        "code_0f1e2d3c4b5a",
        owner="developer",
        branch_name="work/ordinary",
        status="running",
    )
    cw.save_task(smoke)
    cw.save_task(ordinary)

    result = cw.recover_interrupted_agent_runs()

    assert result == {
        "ok": True,
        "recovered": 1,
        "tasks": ["code_0f1e2d3c4b5a"],
        "paused_smoke": 1,
        "paused_smoke_tasks": ["code_a1b2c3d4e5f6"],
    }
    saved_smoke = cw.load_task("code_a1b2c3d4e5f6")
    assert saved_smoke["agent_status"] == "paused"
    assert saved_smoke["agent_auto_resume_pending"] is False
    assert saved_smoke["agent_stop_requested"] is False
    assert saved_smoke["agent_pause_requested"] is False
    assert saved_smoke["agent_error"] == ""
    assert saved_smoke["agent_stop_reason_code"] == "smoke_scheduler_restart"
    assert saved_smoke["agent_finished_at"] > 0
    assert saved_smoke["agent_events"][-1]["type"] == "smoke_scheduler_restart"
    assert saved_smoke["agent_events"][-1]["previous_status"] == "running"
    assert saved_smoke["agent_runs"][-1]["status"] == "paused"
    assert saved_smoke["agent_runs"][-1]["error"] == ""
    assert saved_smoke["agent_runs"][-1]["stop_reason_code"] == (
        "smoke_scheduler_restart"
    )

    saved_ordinary = cw.load_task("code_0f1e2d3c4b5a")
    assert saved_ordinary["agent_status"] == "interrupted"
    assert saved_ordinary["agent_auto_resume_pending"] is True
    assert saved_ordinary["agent_stop_reason_code"] == "gateway_restart"


def test_recovery_excludes_already_interrupted_scheduler_smoke(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    smoke = _task(
        "code_123456abcdef",
        owner="coding-smoke-scheduler",
        branch_name="nexus-coding-smoke/456-def456",
        status="interrupted",
    )
    cw.save_task(smoke)

    result = cw.recover_interrupted_agent_runs()

    assert result["recovered"] == 0
    assert result["tasks"] == []
    assert result["paused_smoke_tasks"] == ["code_123456abcdef"]
    saved = cw.load_task("code_123456abcdef")
    assert saved["agent_status"] == "paused"
    assert saved["agent_auto_resume_pending"] is False
    assert saved["agent_stop_reason_code"] == "smoke_scheduler_restart"
