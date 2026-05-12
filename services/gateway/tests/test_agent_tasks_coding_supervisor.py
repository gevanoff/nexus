from __future__ import annotations

import os
import sys
import types

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import agent_tasks


def _insert_task(metadata: dict[str, object]) -> object:
    with agent_tasks._connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (
                id, title, prompt, agent, kind, status, next_run_ts, run_count, created_ts, updated_ts, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "task-supervisor",
                "Coding Workspace Supervisor",
                "monitor coding workspaces",
                "scheduled_llm_supervisor",
                "interval",
                "enabled",
                1_700_000_000,
                7,
                1_700_000_000,
                1_700_000_000,
                agent_tasks.json.dumps(metadata, separators=(",", ":"), sort_keys=True),
            ),
        )
        return conn.execute("SELECT * FROM agent_tasks WHERE id=?", ("task-supervisor",)).fetchone()


def test_scheduled_prompt_includes_preface(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    row = _insert_task({})

    prompt = agent_tasks._scheduled_prompt(row, 1_700_000_000, preface="Automatic preface")

    assert prompt.startswith("Automatic preface\n\nA scheduled Nexus agent task is due.")


@pytest.mark.asyncio
async def test_auto_recover_coding_supervisor_resumes_stopped_task(monkeypatch, tmp_path):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()

    resumed: list[tuple[str, str]] = []

    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "ok": True,
            "tasks": [
                {
                    "id": "code_123",
                    "status": "ready",
                    "agent": {"status": "stopped"},
                    "safe_actions": ["resume", "guide_and_resume"],
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        resumed.append((task_id, actor or ""))
        return {"agent": {"status": "queued"}}

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)

    row = _insert_task(
        {
            "supervisor_kind": "coding_workspace_supervisor",
            "auto_recovery": {"limit": 2, "cooldown_sec": 1800, "include_failed": True},
        }
    )

    result = await agent_tasks._auto_recover_coding_supervisor(row)

    assert result["enabled"] is True
    assert result["actions"] == [
        {
            "task_id": "code_123",
            "action": "resume",
            "previous_status": "stopped",
            "agent_status": "queued",
        }
    ]
    assert resumed == [("code_123", "coding-supervisor-auto")]

    stored = agent_tasks.get_task({"id": "task-supervisor"})
    assert stored["ok"] is True
    attempts = stored["task"]["metadata"]["auto_recovery_state"]["attempts"]
    assert attempts["code_123"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_auto_recover_coding_supervisor_honors_cooldown(monkeypatch, tmp_path):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()

    now = 1_700_000_000
    monkeypatch.setattr(agent_tasks, "_now", lambda: now)

    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "ok": True,
            "tasks": [
                {
                    "id": "code_123",
                    "status": "ready",
                    "agent": {"status": "failed"},
                    "safe_actions": ["resume", "guide_and_resume"],
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        raise AssertionError("start_agent_run should not be called during cooldown")

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)

    row = _insert_task(
        {
            "supervisor_kind": "coding_workspace_supervisor",
            "auto_recovery": {"cooldown_sec": 1800, "include_failed": True},
            "auto_recovery_state": {
                "attempts": {
                    "code_123": {"attempt_count": 1, "last_resume_ts": now - 60, "last_status": "failed"}
                }
            },
        }
    )

    result = await agent_tasks._auto_recover_coding_supervisor(row)

    assert result["enabled"] is True
    assert result["actions"] == []
    assert result["skipped"] == [{"task_id": "code_123", "reason": "cooldown", "retry_after_sec": 1740}]