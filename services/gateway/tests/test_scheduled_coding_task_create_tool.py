from __future__ import annotations

import json
import os
import sys
import types

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import app
from app import agent_runtime_v1 as ar
from app import agent_tasks
from app import tools_bus
from app import ui_routes


class _JsonRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def json(self) -> dict[str, object]:
        return self._payload


def test_coding_task_create_is_selectable_but_default_off_for_scheduled_tasks():
    assert "coding_task_create" in ar.tools_for_tier(1)
    assert "coding_task_create" not in ui_routes._default_task_tools(1)
    assert "coding workspaces" in ui_routes._task_tool_default_off_reason("coding_task_create")

    selected = ui_routes._coerce_selected_tools(["coding_task_create"], 1)

    assert selected == ["coding_task_create", "tool_manifest"]
    assert ar._request_is_heavy(tier=1, tools_allowlist=selected) is False


def test_coder_task_type_is_enabled_and_requires_create_tool():
    task_types = {item["id"]: item for item in ui_routes._task_type_capabilities()}

    assert task_types["coder"]["enabled"] is True
    assert task_types["coder"]["default_model"] == "coder"
    assert task_types["coder"]["default_tier"] == 1
    assert task_types["coder"]["required_tools"] == []
    assert task_types["coder"]["default_tools"] == ["coding_task_create", "tool_manifest"]
    modes = {item["id"]: item for item in task_types["coder"]["coding_modes"]}
    assert modes["agent"]["required_tools"] == ["coding_task_create"]
    assert modes["review_audit"]["required_tools"] == ["coding_task_create"]
    assert modes["ops_diagnostics"]["required_tools"] == ["coding_task_create"]
    assert modes["model_integration"]["required_tools"] == ["coding_model_integration"]
    assert ui_routes._task_type_default_tools("coder", 1, coding_mode="model_integration") == [
        "coding_model_integration",
        "tool_manifest",
    ]


@pytest.mark.asyncio
async def test_create_coder_scheduled_task_persists_coding_tool_defaults(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    specs_path = tmp_path / "agent_specs.json"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    monkeypatch.setattr(ui_routes, "_scheduled_agent_specs_path", lambda: specs_path)
    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda req: None)
    monkeypatch.setattr(
        ui_routes,
        "_require_user",
        lambda req: types.SimpleNamespace(id=42, username="paper", admin=True),
    )
    agent_tasks.init_db()

    result = await ui_routes.ui_api_agent_tasks_create(
        _JsonRequest(
            {
                "task_type": "coder",
                "title": "Scheduled implementation",
                "prompt": "Implement the requested repository change.",
                "delay_seconds": 60,
            }
        )
    )

    assert result["ok"] is True
    task = result["task"]
    meta = task["metadata"]
    assert meta["task_type"] == "coder"
    assert meta["model"] == "coder"
    assert meta["tier"] == 1
    assert meta["tools"] == ["coding_task_create", "tool_manifest"]
    assert meta["future"]["outputs"] == ["coding_workspace"]
    assert task["agent"].startswith("scheduled_coder_")

    specs = json.loads(specs_path.read_text(encoding="utf-8"))
    spec = specs[meta["agent_spec"]]
    assert spec["model"] == "coder"
    assert spec["tier"] == 1
    assert spec["tools_allowlist"] == ["coding_task_create", "tool_manifest"]


def test_scheduled_coder_prompt_instructs_workspace_creation(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    created = agent_tasks.create_task(
        {
            "title": "Coder run",
            "prompt": "Fix the failing tests.",
            "agent": "scheduled_coder_test",
            "delay_seconds": 60,
            "metadata": {"task_type": "coder"},
        }
    )
    assert created["ok"] is True

    with agent_tasks._connect() as conn:
        row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (created["task"]["id"],)).fetchone()

    prompt = agent_tasks._scheduled_prompt(row, 1_700_000_000)

    assert "Coder task instructions:" in prompt
    assert "coding_task_create" in prompt
    assert "Set auto_run=true" in prompt


@pytest.mark.asyncio
async def test_create_model_integration_scheduled_task_uses_model_integration_tool(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    specs_path = tmp_path / "agent_specs.json"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    monkeypatch.setattr(ui_routes, "_scheduled_agent_specs_path", lambda: specs_path)
    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda req: None)
    monkeypatch.setattr(
        ui_routes,
        "_require_user",
        lambda req: types.SimpleNamespace(id=42, username="paper", admin=True),
    )
    agent_tasks.init_db()

    result = await ui_routes.ui_api_agent_tasks_create(
        _JsonRequest(
            {
                "task_type": "coder",
                "coding_mode": "model_integration",
                "title": "Scheduled model integration",
                "prompt": "",
                "delay_seconds": 60,
                "model_integration": {
                    "model": "owner/model",
                    "repo_url": "https://github.com/gevanoff/nexus.git",
                    "preferred_runtime": "vllm",
                    "route_kind": "chat",
                },
            }
        )
    )

    assert result["ok"] is True
    task = result["task"]
    meta = task["metadata"]
    assert task["prompt"] == "Integrate the specified model into Nexus."
    assert meta["coding_mode"] == "model_integration"
    assert meta["tools"] == ["coding_model_integration", "tool_manifest"]
    assert meta["future"]["outputs"] == ["model_integration_workspace"]
    assert meta["model_integration"]["model"] == "owner/model"

    specs = json.loads(specs_path.read_text(encoding="utf-8"))
    assert specs[meta["agent_spec"]]["tools_allowlist"] == ["coding_model_integration", "tool_manifest"]


def test_scheduled_model_integration_prompt_instructs_model_integration_tool(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(agent_tasks, "_db_path", lambda: str(db_path))
    agent_tasks.init_db()
    created = agent_tasks.create_task(
        {
            "title": "Model integration",
            "prompt": "Keep the route OpenAI-compatible.",
            "agent": "scheduled_coder_test",
            "delay_seconds": 60,
            "metadata": {
                "task_type": "coder",
                "coding_mode": "model_integration",
                "model_integration": {
                    "model": "owner/model",
                    "repo_url": "https://github.com/gevanoff/nexus.git",
                    "preferred_runtime": "vllm",
                },
            },
        }
    )
    assert created["ok"] is True

    with agent_tasks._connect() as conn:
        row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (created["task"]["id"],)).fetchone()

    prompt = agent_tasks._scheduled_prompt(row, 1_700_000_000)

    assert "coding_model_integration" in prompt
    assert '"model":"owner/model"' in prompt
    assert '"repo_url":"https://github.com/gevanoff/nexus.git"' in prompt


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
