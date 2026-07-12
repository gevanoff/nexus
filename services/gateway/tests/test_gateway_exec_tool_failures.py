import json
import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage
from app.tool_calling.executor import resolve_execution_policy, run_gateway_tool_loop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [("unknown_tool", "{}", "unknown_tool"), ("nexus_health", "{bad", "invalid_arguments")],
)
async def test_tool_failures_become_tool_results_instead_of_500(monkeypatch, tmp_path, name, arguments, expected):
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLSETS", "core")
    monkeypatch.setattr(S, "NEXUS_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    calls = 0

    async def upstream(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"model": "model", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_bad", "type": "function", "function": {"name": name, "arguments": arguments}}]}, "finish_reason": "tool_calls"}]}
        result = json.loads(req.messages[-1].content)
        assert result["error"] == expected
        return {"model": "model", "choices": [{"message": {"role": "assistant", "content": "recovered"}, "finish_reason": "stop"}]}

    alias = ModelAlias(backend="local_vllm", upstream_model="model", tools=True)
    req = ChatCompletionRequest(model="default", messages=[ChatMessage(role="user", content="test")], x_nexus={"tool_execution_mode": "gateway_exec"})
    result = await run_gateway_tool_loop(req, policy=resolve_execution_policy(req, alias), alias=alias, call_backend=upstream, request_id="failure")
    assert result.response["choices"][0]["message"]["content"] == "recovered"


@pytest.mark.asyncio
async def test_max_tool_rounds_stops_repeating_model(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLSETS", "core")
    monkeypatch.setattr(S, "NEXUS_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    async def upstream(_req):
        return {"model": "model", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_loop", "type": "function", "function": {"name": "nexus_health", "arguments": '{"include_upstreams":false,"include_models":false}'}}]}, "finish_reason": "tool_calls"}]}

    alias = ModelAlias(backend="local_vllm", upstream_model="model", tools=True)
    req = ChatCompletionRequest(model="default", messages=[ChatMessage(role="user", content="loop")], x_nexus={"tool_execution_mode": "gateway_exec", "max_tool_rounds": 2})
    result = await run_gateway_tool_loop(req, policy=resolve_execution_policy(req, alias), alias=alias, call_backend=upstream, request_id="loop")
    assert result.stopped_reason == "max_tool_rounds"
    assert "stopped tool execution" in result.response["choices"][0]["message"]["content"]
