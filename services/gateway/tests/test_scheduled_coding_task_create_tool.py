from __future__ import annotations

import json
import os
import sys
import types

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import app
from app import agent_runtime_v1 as ar
from app import tools_bus
from app import ui_routes


def test_coding_task_create_is_selectable_but_default_off_for_scheduled_tasks():
    assert "coding_task_create" in ar.tools_for_tier(1)
    assert "coding_task_create" not in ui_routes._default_task_tools(1)
    assert "coding workspaces" in ui_routes._task_tool_default_off_reason("coding_task_create")

    selected = ui_routes._coerce_selected_tools(["coding_task_create"], 1)

    assert selected == ["coding_task_create", "tool_manifest"]
    assert ar._request_is_heavy(tier=1, tools_allowlist=selected) is False


def test_coding_task_create_tool_creates_workspace(monkeypatch):
    created: dict[str, object] = {}

    def _create_task(**kwargs: object) -> dict[str, object]:
        created.update(kwargs)
        return {
            "id": "code_123",
            "status": "ready",
            "repo_url": kwargs.get("repo_url"),
            "branch_name": kwargs.get("branch_name"),
            "prompt": kwargs.get("prompt"),
            "coding_model": kwargs.get("coding_model"),
        }

    monkeypatch.setattr(tools_bus.coding_workspace, "create_task", _create_task)

    result = tools_bus.run_tool_call(
        "coding_task_create",
        json.dumps(
            {
                "repo_url": "https://github.com/example/repo.git",
                "base_branch": "main",
                "branch_name": "agent/work",
                "prompt": "Implement the scheduled workspace feature.",
                "coding_model": "coder",
            }
        ),
        allowed_tools={"coding_task_create"},
    )

    assert result["ok"] is True
    assert result["task"]["id"] == "code_123"
    assert created["repo_url"] == "https://github.com/example/repo.git"
    assert created["base_branch"] == "main"
    assert created["branch_name"] == "agent/work"
    assert created["prompt"] == "Implement the scheduled workspace feature."
    assert created["owner"] == "scheduled-task-tool"
    assert created["coding_model"] == "coder"


def test_coding_task_create_tool_can_auto_run_workspace(monkeypatch):
    started: list[dict[str, object]] = []

    def _create_task(**kwargs: object) -> dict[str, object]:
        return {
            "id": "code_456",
            "status": "ready",
            "coding_model": kwargs.get("coding_model"),
        }

    async def _start_agent_run(task_id: str, **kwargs: object) -> dict[str, object]:
        started.append({"task_id": task_id, **kwargs})
        return {
            "id": task_id,
            "status": "ready",
            "agent": {"status": "queued"},
            "coding_model": kwargs.get("coding_model"),
        }

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    monkeypatch.setattr(tools_bus.coding_workspace, "create_task", _create_task)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)

    result = tools_bus.run_tool_call(
        "coding_task_create",
        json.dumps(
            {
                "prompt": "Make the focused change and run tests.",
                "coding_model": "coder",
                "auto_run": True,
                "auto_commit": True,
                "commit_message": "Implement scheduled coding workspace creation",
            }
        ),
        allowed_tools={"coding_task_create"},
    )

    assert result["ok"] is True
    assert result["task"]["agent"]["status"] == "queued"
    assert started == [
        {
            "task_id": "code_456",
            "git_token_value": None,
            "coding_model": "coder",
            "auto_commit": True,
            "commit_message": "Implement scheduled coding workspace creation",
            "actor": "scheduled-task-tool",
        }
    ]


def test_coding_task_create_tool_fails_closed_when_not_selected():
    result = tools_bus.run_tool_call(
        "coding_task_create",
        json.dumps({"prompt": "Do work"}),
        allowed_tools={"tool_manifest"},
    )

    assert result["ok"] is False
    assert result["error_type"] == "unknown_tool"
