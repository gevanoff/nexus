from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_routes


class _Request:
    pass


@pytest.mark.asyncio
async def test_v1_coding_monitor_returns_workspace_monitor(monkeypatch):
    async def _recover():
        return {"ok": True, "recovered": 0, "tasks": []}

    def _monitor_tasks(*, limit, only_attention, stalled_after_sec):
        return {
            "ok": True,
            "args": {
                "limit": limit,
                "only_attention": only_attention,
                "stalled_after_sec": stalled_after_sec,
            },
        }

    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "recover_stale_agent_runs", _recover)
    monkeypatch.setattr(coding_routes.cw, "monitor_tasks", _monitor_tasks)

    result = await coding_routes.v1_coding_monitor(
        _Request(),
        limit=7,
        only_attention=True,
        stalled_after_sec=180.0,
    )

    assert result["ok"] is True
    assert result["args"] == {"limit": 7, "only_attention": True, "stalled_after_sec": 180.0}


@pytest.mark.asyncio
async def test_v1_coding_inspect_recovers_and_inspects_task(monkeypatch):
    recovered = []

    async def _recover(task_id):
        recovered.append(task_id)
        return {"id": task_id, "agent": {"status": "paused"}}

    def _inspect(task_id, *, stalled_after_sec):
        return {"ok": True, "task": {"id": task_id, "stalled_after_sec": stalled_after_sec}}

    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "recover_stale_agent_run", _recover)
    monkeypatch.setattr(coding_routes.cw, "inspect_task", _inspect)

    result = await coding_routes.v1_coding_inspect_task(_Request(), "code_123", stalled_after_sec=240.0)

    assert recovered == ["code_123"]
    assert result == {"ok": True, "task": {"id": "code_123", "stalled_after_sec": 240.0}}


@pytest.mark.asyncio
async def test_v1_coding_intervene_guidance_appends_message(monkeypatch):
    appended = []

    async def _recover(task_id):
        return {"id": task_id, "agent": {"status": "paused"}}

    def _append(task_id, *, message, actor):
        appended.append((task_id, message, actor))
        return {"id": task_id, "guidance_messages": [{"message": message, "actor": actor}]}

    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "recover_stale_agent_run", _recover)
    monkeypatch.setattr(coding_routes.cw, "append_guidance_message", _append)

    result = await coding_routes.v1_coding_intervene(
        _Request(),
        "code_123",
        coding_routes.CodingInterventionRequest(action="guidance", message="Please continue.", actor="supervisor"),
    )

    assert result["ok"] is True
    assert result["started"] is False
    assert appended == [("code_123", "Please continue.", "supervisor")]


@pytest.mark.asyncio
async def test_v1_coding_intervene_guide_and_resume_starts_inactive_task(monkeypatch):
    calls = []

    async def _recover(task_id):
        return {"id": task_id, "coding_model": "coder", "agent": {"status": "failed"}}

    def _append(task_id, *, message, actor):
        calls.append(("append", task_id, message, actor))
        return {"id": task_id}

    async def _start_agent_run(task_id, **kwargs):
        calls.append(("start", task_id, kwargs))
        return {"id": task_id, "agent": {"status": "queued"}}

    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "recover_stale_agent_run", _recover)
    monkeypatch.setattr(coding_routes.ca, "start_agent_run", _start_agent_run)
    monkeypatch.setattr(coding_routes.cw, "append_guidance_message", _append)

    result = await coding_routes.v1_coding_intervene(
        _Request(),
        "code_123",
        coding_routes.CodingInterventionRequest(
            action="guide_and_resume",
            message="Fix validation and continue.",
            actor="supervisor",
            coding_model="coder",
            auto_commit=True,
            commit_message="checkpoint",
        ),
    )

    assert result["ok"] is True
    assert result["started"] is True
    assert calls[0] == ("append", "code_123", "Fix validation and continue.", "supervisor")
    assert calls[1][0:2] == ("start", "code_123")
    assert calls[1][2]["coding_model"] == "coder"
    assert calls[1][2]["auto_commit"] is True
    assert calls[1][2]["commit_message"] == "checkpoint"


@pytest.mark.asyncio
async def test_v1_coding_intervene_pause_requests_pause(monkeypatch):
    async def _recover(task_id):
        return {"id": task_id, "agent": {"status": "running"}}

    async def _pause(task_id):
        return {"id": task_id, "agent": {"status": "pausing"}}

    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "recover_stale_agent_run", _recover)
    monkeypatch.setattr(coding_routes.ca, "request_pause", _pause)

    result = await coding_routes.v1_coding_intervene(
        _Request(),
        "code_123",
        coding_routes.CodingInterventionRequest(action="stop"),
    )

    assert result == {"ok": True, "action": "pause", "started": False, "task": {"id": "code_123", "agent": {"status": "pausing"}}}


def test_run_horizon_kwargs_forwards_optional_limits():
    body = coding_routes.CodingAgentRunRequest(
        max_cycles=120,
        max_runtime_sec=7200,
        context_reset_cycles=10,
    )

    assert coding_routes._run_horizon_kwargs(body) == {
        "max_cycles": 120,
        "max_runtime_sec": 7200,
        "context_reset_cycles": 10,
    }


@pytest.mark.asyncio
async def test_v1_coding_plan_updates_durable_workspace_plan(monkeypatch):
    calls = []

    def _update(task_id, *, goal, items, note, actor):
        calls.append((task_id, goal, items, note, actor))
        return {"ok": True, "plan": {"goal": goal, "items": items}}

    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.cw, "update_project_plan", _update)
    monkeypatch.setattr(coding_routes.cw, "load_task", lambda task_id: {"id": task_id})
    monkeypatch.setattr(coding_routes.cw, "public_task", lambda task: {**task, "public": True})

    result = await coding_routes.v1_coding_task_plan(
        _Request(),
        "code_123",
        coding_routes.CodingProjectPlanRequest(
            goal="Ship the feature",
            items=[{"id": "verify", "title": "Verify", "status": "pending"}],
            note="Keep this durable.",
        ),
    )

    assert calls == [
        (
            "code_123",
            "Ship the feature",
            [{"id": "verify", "title": "Verify", "status": "pending"}],
            "Keep this durable.",
            "api",
        )
    ]
    assert result["task"] == {"id": "code_123", "public": True}
