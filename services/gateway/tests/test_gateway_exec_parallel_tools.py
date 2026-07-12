import json
import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage
from app.tool_calling.executor import resolve_execution_policy, run_gateway_tool_loop


@pytest.mark.asyncio
async def test_gateway_exec_runs_two_parallel_read_only_tools(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha", encoding="utf-8")
    (repo / "b.txt").write_text("beta", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLSETS", "repo")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(repo))
    monkeypatch.setattr(S, "NEXUS_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    calls = 0

    async def backend(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_calls = [
                {"id": f"call_{name}", "type": "function", "function": {"name": "nexus_file_read", "arguments": json.dumps({"path": f"{name}.txt", "start_line": None, "end_line": None, "max_chars": 100})}}
                for name in ("a", "b")
            ]
            return {"model": "upstream", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}, "finish_reason": "tool_calls"}]}
        assert [message.role for message in req.messages[-2:]] == ["tool", "tool"]
        return {"model": "upstream", "choices": [{"message": {"role": "assistant", "content": "Read both."}, "finish_reason": "stop"}]}

    alias = ModelAlias(backend="local_mlx", upstream_model="upstream", tools=True)
    req = ChatCompletionRequest(model="coder", messages=[ChatMessage(role="user", content="Read both")], x_nexus={"tool_execution_mode": "gateway_exec", "toolsets": ["repo"]})
    result = await run_gateway_tool_loop(req, policy=resolve_execution_policy(req, alias), alias=alias, call_backend=backend, request_id="req-2")
    assert result.tools_executed == ("nexus_file_read", "nexus_file_read")
    assert result.stopped_reason == "final_answer"
