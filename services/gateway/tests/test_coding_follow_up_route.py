from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import coding_routes_guarded as guarded_routes


@pytest.mark.asyncio
async def test_integrated_workspace_can_create_fresh_follow_up(monkeypatch):
    source = {
        "id": "code_abcdef123456",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "coding_model": "coder",
        "agent_stop_reason_code": "work_already_integrated",
        "terminal_result": {"stop_reason_code": "work_already_integrated"},
    }
    captured = {}

    monkeypatch.setattr(guarded_routes.routes, "_require_coding_ui", lambda req: SimpleNamespace(id=7, username="tester"))
    monkeypatch.setattr(guarded_routes.cw, "load_task", lambda task_id: source)

    async def fake_create(req, body):
        captured["body"] = body
        return {"task": {"id": "code_fedcba654321", "status": "ready"}}

    monkeypatch.setattr(guarded_routes.routes, "ui_coding_create_task", fake_create)

    result = await guarded_routes.ui_coding_create_follow_up(
        SimpleNamespace(),
        source["id"],
        guarded_routes.CodingFollowUpRequest(prompt="Implement the next lifecycle improvement."),
    )

    body = captured["body"]
    assert body.repo_url == source["repo_url"]
    assert body.base_branch == "main"
    assert body.branch_name is None
    assert body.prompt == "Implement the next lifecycle improvement."
    assert result["source_task_id"] == source["id"]
    assert result["action"] == "created_follow_up_workspace"
    assert result["task"]["id"] != source["id"]


@pytest.mark.asyncio
async def test_follow_up_endpoint_rejects_non_integrated_workspace(monkeypatch):
    source = {
        "id": "code_abcdef123456",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "agent_stop_reason_code": "no_progress_limit",
        "terminal_result": {"stop_reason_code": "no_progress_limit"},
    }
    monkeypatch.setattr(guarded_routes.routes, "_require_coding_ui", lambda req: SimpleNamespace(id=7, username="tester"))
    monkeypatch.setattr(guarded_routes.cw, "load_task", lambda task_id: source)

    with pytest.raises(HTTPException) as exc_info:
        await guarded_routes.ui_coding_create_follow_up(
            SimpleNamespace(),
            source["id"],
            guarded_routes.CodingFollowUpRequest(prompt="New work"),
        )

    assert exc_info.value.status_code == 409
