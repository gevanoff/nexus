import json
import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage
from app.tool_calling.executor import resolve_execution_policy, run_gateway_tool_loop


@pytest.mark.asyncio
async def test_gateway_exec_runs_health_and_continues_to_final_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLSETS", "core")
    monkeypatch.setattr(S, "NEXUS_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    calls = 0

    async def backend(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert any((tool.get("function") or {}).get("name") == "nexus_health" for tool in req.tools)
            return {"model": "upstream", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_health", "type": "function", "function": {"name": "nexus_health", "arguments": json.dumps({"include_upstreams": False, "include_models": False})}}]}, "finish_reason": "tool_calls"}]}
        tool_message = req.messages[-1]
        assert tool_message.role == "tool"
        assert json.loads(tool_message.content)["ok"] is True
        return {"model": "upstream", "choices": [{"message": {"role": "assistant", "content": "Gateway is healthy."}, "finish_reason": "stop"}]}

    alias = ModelAlias(backend="local_vllm", upstream_model="upstream", tools=True)
    req = ChatCompletionRequest(model="default", messages=[ChatMessage(role="user", content="Check health")], x_nexus={"tool_execution_mode": "gateway_exec"})
    policy = resolve_execution_policy(req, alias)
    result = await run_gateway_tool_loop(req, policy=policy, alias=alias, call_backend=backend, request_id="req-1")

    assert result.response["choices"][0]["message"]["content"] == "Gateway is healthy."
    assert result.tools_executed == ("nexus_health",)
    assert calls == 2
    assert (tmp_path / "audit.jsonl").exists()
