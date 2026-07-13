from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import tools_bus
from app.agent_api.auth import AgentToolCaller, agent_tool_caller_from_request
from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage
from app.tool_calling.executor import resolve_execution_policy, run_gateway_tool_loop
from app.tool_calling.registry import builtin_tool_definitions


def _session_caller() -> AgentToolCaller:
    return AgentToolCaller(user=SimpleNamespace(id=7, username="model-user"), token=None, allow_session_user=True)


@pytest.mark.asyncio
async def test_gateway_exec_propagates_caller_to_agent_api_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    monkeypatch.setattr(S, "NEXUS_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    calls = 0

    async def backend(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert any((tool.get("function") or {}).get("name") == "nexus_agent_api" for tool in req.tools)
            arguments = {"operation": "me", "workspace_id": None, "task_id": None, "parameters": {}}
            return {
                "model": "local",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_agent_api",
                            "type": "function",
                            "function": {"name": "nexus_agent_api", "arguments": json.dumps(arguments)},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }
        result = json.loads(req.messages[-1].content)
        assert result["ok"] is True
        assert result["data"]["user_id"] == 7
        return {"model": "local", "choices": [{"message": {"role": "assistant", "content": "Ready."}, "finish_reason": "stop"}]}

    alias = ModelAlias(backend="local_vllm", upstream_model="local", tools=True)
    request = ChatCompletionRequest(
        model="default",
        messages=[ChatMessage(role="user", content="Inspect my Agent API identity")],
        x_nexus={"tool_execution_mode": "gateway_exec", "toolsets": ["workspace"]},
    )
    result = await run_gateway_tool_loop(
        request,
        policy=resolve_execution_policy(request, alias),
        alias=alias,
        call_backend=backend,
        request_id="req-agent-api",
        caller=_session_caller(),
    )
    assert result.tools_executed == ("nexus_agent_api",)
    assert result.response["choices"][0]["message"]["content"] == "Ready."


def test_legacy_tool_bus_propagates_caller_to_agent_api_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(S, "TOOLS_LOG_PATH", str(tmp_path / "tools.jsonl"))
    result = tools_bus.run_tool_call(
        "nexus_agent_api",
        json.dumps({"operation": "me", "workspace_id": None, "task_id": None, "parameters": {}}),
        allowed_tools={"nexus_agent_api"},
        caller=_session_caller(),
    )
    assert result["ok"] is True
    assert result["data"]["user_id"] == 7


def test_agent_api_tool_is_registered_in_both_tool_systems() -> None:
    definition = builtin_tool_definitions()["nexus_agent_api"]
    assert definition.toolset == "workspace"
    assert definition.uses_caller_context is True
    assert "nexus_agent_api" in tools_bus.TOOL_SCHEMAS


def test_synthetic_requests_without_fastapi_state_are_unauthenticated() -> None:
    caller = agent_tool_caller_from_request(SimpleNamespace())
    assert caller.user is None
    assert caller.token is None
