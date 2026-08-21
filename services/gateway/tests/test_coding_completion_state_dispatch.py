from __future__ import annotations

from fastapi import HTTPException

from app import coding_completion_state_dispatch as completion_dispatch
from app import coding_completion_state_hardening as hardening


NOTE = (
    "Root cause: current hypothesis.\n"
    "Repository evidence: app.py:1-10\n"
    "Competing explanation checked: alternate path.\n"
    "Expected result: targeted repair."
)


class _CW:
    def __init__(self):
        self.tasks: dict[str, dict] = {}

    def load_task(self, task_id: str):
        if task_id not in self.tasks:
            raise HTTPException(status_code=404, detail="coding task not found")
        return self.tasks[task_id]


class _Agent:
    def __init__(self):
        self.calls: list[str] = []
        self._run_tool = self._hardened

    def _hardened(self, _task_id, _name, _args, *, git_token_value):
        assert git_token_value is None
        self.calls.append("hardened")
        return {"ok": True, "path": "hardened"}


class _Guarded:
    def __init__(self, agent: _Agent):
        self.calls: list[str] = []
        self._run_tool_with_semantic_acceptance = self._established
        self._agent = agent

    def _established(self, _task_id, _name, _args, *, git_token_value):
        assert git_token_value is None
        self.calls.append("established")
        return {"ok": True, "path": "established"}


def _active_task() -> dict:
    return {
        "project_plan": {"revision": 2, "note": NOTE},
    }


def _consumed_task() -> dict:
    task = _active_task()
    task[hardening._LIFECYCLE_KEY] = {
        "schema": "nexus_coding_hypothesis_lifecycle.v1",
        "status": hardening._CONSUMED_STATUS,
        "plan_revision": 2,
        "note_fingerprint": hardening._note_fingerprint(NOTE),
        "verified_evidence_digest": "first pre-edit evidence",
    }
    return task


def test_dispatch_keeps_agent_and_guarded_aliases_identical():
    cw = _CW()
    cw.tasks["active"] = _active_task()
    agent = _Agent()
    guarded = _Guarded(agent)

    completion_dispatch.install(agent, guarded, cw, hardening)

    assert agent._run_tool is guarded._run_tool_with_semantic_acceptance
    result = agent._run_tool(
        "active",
        "coding_replace_text",
        {},
        git_token_value=None,
    )
    assert result["path"] == "hardened"
    assert agent.calls == ["hardened"]
    assert guarded.calls == []


def test_dispatch_preserves_first_consumption_by_bypassing_lifecycle_rewrite():
    cw = _CW()
    task = _consumed_task()
    original_lifecycle = dict(task[hardening._LIFECYCLE_KEY])
    cw.tasks["consumed"] = task
    agent = _Agent()
    guarded = _Guarded(agent)

    completion_dispatch.install(agent, guarded, cw, hardening)
    result = agent._run_tool(
        "consumed",
        "coding_replace_text",
        {},
        git_token_value=None,
    )

    assert result["path"] == "established"
    assert guarded.calls == ["established"]
    assert agent.calls == []
    assert cw.tasks["consumed"][hardening._LIFECYCLE_KEY] == original_lifecycle


def test_dispatch_preserves_synthetic_direct_call_compatibility():
    cw = _CW()
    agent = _Agent()
    guarded = _Guarded(agent)

    completion_dispatch.install(agent, guarded, cw, hardening)
    result = guarded._run_tool_with_semantic_acceptance(
        "synthetic-task",
        "coding_finish",
        {"success": False},
        git_token_value=None,
    )

    assert result["path"] == "established"
    assert guarded.calls == ["established"]
    assert agent.calls == []
