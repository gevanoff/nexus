from __future__ import annotations

import os
import sys
import types

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import app
from app import agent_tasks
from app import nexus_hardware


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


def _get_row() -> object:
    with agent_tasks._connect() as conn:
        return conn.execute("SELECT * FROM agent_tasks WHERE id=?", ("task-supervisor",)).fetchone()


def test_scheduled_prompt_includes_preface(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    monkeypatch.setattr(nexus_hardware, "_RUNTIME_SNAPSHOT", None)
    monkeypatch.setattr(nexus_hardware.S, "NEXUS_HARDWARE_SNAPSHOT_PATH", str(tmp_path / "missing_hardware_snapshot.json"))
    agent_tasks.init_db()
    row = _insert_task({})

    prompt = agent_tasks._scheduled_prompt(row, 1_700_000_000, preface="Automatic preface")

    assert prompt.startswith("Automatic preface\n\nA scheduled Nexus agent task is due.")
    assert "Nexus production host hardware context" in prompt
    assert "ai2: macos_arm64; Apple M3 Ultra" in prompt
    assert "ai1: linux_x86_64; 12th Gen Intel Core i7-12700F" in prompt
    assert "NVIDIA GeForce RTX 3090 24 GiB; NVIDIA GeForce RTX 3090 24 GiB" in prompt
    assert "ada2: linux_x86_64; 13th Gen Intel Core i7-13700K" in prompt
    assert "meltdown: linux_x86_64; Intel Core i7-5930K" in prompt
    assert "NVIDIA GeForce RTX 5060 Ti" in prompt
    assert "migraine: macos_arm64; Apple M2" in prompt
    assert "not part of the current production model-serving topology" in prompt


def test_coding_supervisor_tasks_are_protected(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    _insert_task({"supervisor_kind": "coding_workspace_supervisor"})

    task = agent_tasks.get_task({"id": "task-supervisor"})["task"]

    assert task["metadata"]["protected"] is True
    assert "Protected supervisor task" in task["metadata"]["protected_reason"]


def test_coding_supervisor_cannot_be_cancelled_or_run_manually(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    _insert_task({"supervisor_kind": "coding_workspace_supervisor"})

    cancel = agent_tasks.cancel_task({"id": "task-supervisor"})
    run_now = agent_tasks.run_task_now({"id": "task-supervisor"})

    assert cancel["ok"] is False
    assert "protected" in cancel["error"]
    assert run_now["ok"] is False
    assert "protected" in run_now["error"]


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
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)

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
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)

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


@pytest.mark.asyncio
async def test_auto_recover_coding_supervisor_sends_recovery_notification_once(monkeypatch, tmp_path):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()

    now = 1_700_000_000
    monkeypatch.setattr(agent_tasks, "_now", lambda: now)

    sent: list[tuple[str, str]] = []

    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "ok": True,
            "tasks": [
                {
                    "id": "code_123",
                    "status": "ready",
                    "owner": "alice",
                    "owner_user_id": 7,
                    "attention": ["run_failed"],
                    "recommended_action": "resume",
                    "agent": {"status": "failed"},
                    "safe_actions": ["resume", "guide_and_resume"],
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        return {"agent": {"status": "queued"}}

    async def _send(**kwargs: object):
        sent.append((str(kwargs.get("chat_id") or ""), str(kwargs.get("text") or "")))
        return {"ok": True}

    telegram_notifications = types.SimpleNamespace(
        resolve_notification_target=lambda **_: {
            "enabled": True,
            "reason": "ok",
            "chat_id": "12345",
            "mention_username": "alice_tg",
            "notify_on_attention": True,
            "notify_on_recovery": True,
        },
        render_coding_workspace_notification=lambda **kwargs: f"note:{kwargs.get('event_kind')}:{kwargs.get('item', {}).get('id')}",
        send_message=_send,
    )

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setitem(sys.modules, "app.telegram_notifications", telegram_notifications)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)
    monkeypatch.setattr(app, "telegram_notifications", telegram_notifications, raising=False)

    row = _insert_task(
        {
            "supervisor_kind": "coding_workspace_supervisor",
            "auto_recovery": {
                "cooldown_sec": 1800,
                "notification_cooldown_sec": 7200,
            },
        }
    )

    result = await agent_tasks._auto_recover_coding_supervisor(row)

    assert result["actions"] == [
        {
            "task_id": "code_123",
            "action": "resume",
            "previous_status": "failed",
            "agent_status": "queued",
        }
    ]
    assert result["notifications"] == [
        {
            "task_id": "code_123",
            "event": "auto_resume",
            "sent": True,
            "chat_id": "12345",
        }
    ]
    assert sent == [("12345", "note:auto_resume:code_123")]

    stored = agent_tasks.get_task({"id": "task-supervisor"})
    assert stored["task"]["metadata"]["notification_state"]["tasks"]["code_123"]["event"] == "auto_resume"

    sent.clear()
    result_again = await agent_tasks._auto_recover_coding_supervisor(_get_row())

    assert result_again["notifications"] == [
        {
            "task_id": "code_123",
            "event": "needs_attention",
            "sent": False,
            "reason": "cooldown",
            "retry_after_sec": 7200,
        }
    ]
    assert sent == []
