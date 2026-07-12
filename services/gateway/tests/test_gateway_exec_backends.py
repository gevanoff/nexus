import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage
from app.tool_calling.executor import resolve_execution_policy, run_gateway_tool_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["local_mlx", "local_vllm"])
async def test_gateway_execution_loop_is_provider_neutral(monkeypatch, tmp_path, backend):
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLSETS", "core")
    monkeypatch.setattr(S, "NEXUS_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    calls = 0

    async def upstream(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"model": "model", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "nexus_alias_resolve", "arguments": '{"alias":"default"}'}}]}, "finish_reason": "tool_calls"}]}
        assert req.messages[-1].role == "tool"
        return {"model": "model", "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}

    alias = ModelAlias(backend=backend, upstream_model="model", tools=True)
    req = ChatCompletionRequest(model="default", messages=[ChatMessage(role="user", content="resolve default")], x_nexus={"tool_execution_mode": "gateway_exec"})
    result = await run_gateway_tool_loop(req, policy=resolve_execution_policy(req, alias), alias=alias, call_backend=upstream, request_id=backend)
    assert result.response["choices"][0]["message"]["content"] == "done"
    assert result.tools_executed == ("nexus_alias_resolve",)
