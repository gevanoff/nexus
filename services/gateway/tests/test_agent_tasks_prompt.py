from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import agent_tasks


def test_update_task_prompt_changes_future_scheduled_prompt_and_auto_title(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    created = agent_tasks.create_task({"prompt": "review the original task prompt", "delay_seconds": 60})
    task_id = created["task"]["id"]

    result = agent_tasks.update_task_prompt({"id": task_id, "prompt": "review the edited task prompt"})

    assert result["ok"] is True
    assert result["task"]["prompt"] == "review the edited task prompt"
    assert result["task"]["title"] == "review the edited task prompt"
    with agent_tasks._connect() as conn:
        updated_row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    scheduled_prompt = agent_tasks._scheduled_prompt(updated_row, 1_700_000_100)
    assert "Task prompt:\nreview the edited task prompt" in scheduled_prompt


def test_update_task_prompt_rejects_empty_prompt(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    created = agent_tasks.create_task({"prompt": "keep this prompt", "delay_seconds": 60})
    task_id = created["task"]["id"]

    result = agent_tasks.update_task_prompt({"id": task_id, "prompt": "   "})

    assert result == {"ok": False, "error": "prompt must be a non-empty string"}
    assert agent_tasks.get_task({"id": task_id})["task"]["prompt"] == "keep this prompt"
