from __future__ import annotations

import os
import sqlite3
import sys
import types

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from gateway.app import sentinel_runtime


def _sentinel_events(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.sqlite"
    monkeypatch.setattr(sentinel_runtime, "_db_path", lambda: str(db_path))
    sentinel_runtime.init_db()
    return db_path


def _agent_tasks_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(sentinel_runtime.S, "AGENT_TASKS_DB_PATH", str(db_path), raising=False)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE agent_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            agent TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            run_at_ts INTEGER,
            interval_sec INTEGER,
            cron_expr TEXT,
            next_run_ts INTEGER,
            last_run_ts INTEGER,
            last_run_id TEXT,
            last_ok INTEGER,
            last_error TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            max_runs INTEGER,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE agent_task_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            due_ts INTEGER NOT NULL,
            started_ts INTEGER NOT NULL,
            finished_ts INTEGER,
            agent_run_id TEXT,
            ok INTEGER,
            output_text TEXT,
            error TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_sentinel_records_coding_attention_and_auto_resume(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    _agent_tasks_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    resumed = []
    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "counts": {"total": 1, "attention": 1},
            "tasks": [
                {
                    "id": "code_123",
                    "status": "ready",
                    "owner": "alice",
                    "owner_user_id": 7,
                    "needs_attention": True,
                    "attention": ["run_failed"],
                    "safe_actions": ["resume", "guide_and_resume"],
                    "recommended_action": "resume",
                    "recent_events": [{"type": "failed", "summary": "backend error"}],
                    "agent": {"status": "failed", "last_event_age_sec": 300},
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        resumed.append((task_id, actor))
        return {"agent": {"status": "queued"}}

    async def _send_message(**_: object):
        return {"ok": True}

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    telegram_notifications = types.SimpleNamespace(
        resolve_notification_target=lambda **_: {
            "enabled": True,
            "chat_id": "123",
            "mention_username": "alice_tg",
            "notify_on_attention": True,
            "notify_on_recovery": True,
        },
        render_coding_workspace_notification=lambda **kwargs: f"note:{kwargs.get('event_kind')}:{kwargs.get('item', {}).get('id')}",
        send_message=_send_message,
    )

    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setitem(sys.modules, "app.telegram_notifications", telegram_notifications)

    result = await sentinel_runtime.run_monitor_once()

    assert result["summary"]["coding"]["attention"] == 1
    assert result["summary"]["coding"]["actions"] == 1
    assert resumed == [("code_123", "nexus-sentinel-auto")]
    events = sentinel_runtime.list_events(limit=10)
    kinds = {(item["category"], item["event_type"]) for item in events}
    assert ("coding", "needs_attention") in kinds
    assert ("coding", "auto_resume") in kinds


@pytest.mark.asyncio
async def test_sentinel_records_scheduled_task_failures(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    tasks_db = _agent_tasks_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    conn = sqlite3.connect(str(tasks_db))
    conn.execute(
        """
        INSERT INTO agent_tasks (
            id, title, prompt, agent, kind, status, next_run_ts, last_run_ts, last_run_id, last_ok, last_error,
            run_count, max_runs, created_ts, updated_ts, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1",
            "Failure task",
            "run",
            "default",
            "once",
            "error",
            None,
            1_700_000_000,
            "run-1",
            0,
            "backend overloaded",
            1,
            1,
            1_700_000_000,
            1_700_000_000,
            "{}",
        ),
    )
    conn.commit()

    await sentinel_runtime.run_monitor_once()

    conn.execute(
        """
        UPDATE agent_tasks
        SET last_run_id=?, last_ok=?, last_error=?, run_count=?, updated_ts=?
        WHERE id=?
        """,
        ("run-2", 0, "backend overloaded", 2, 1_700_000_000, "task-1"),
    )
    conn.commit()
    conn.close()

    await sentinel_runtime.run_monitor_once()
    events = sentinel_runtime.list_events(limit=20, category="scheduled_tasks")
    assert any(item["event_type"] == "failed" and item["subject_id"] == "task-1" for item in events)