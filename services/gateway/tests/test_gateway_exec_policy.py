import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.model_aliases import ModelAlias
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec
from app.tool_calling.executor import prepare_tools, resolve_execution_policy


def request(**updates):
    values = {"model": "default", "messages": [ChatMessage(role="user", content="hello")]}
    values.update(updates)
    return ChatCompletionRequest(**values)


def test_client_exec_preserves_client_tools(monkeypatch):
    monkeypatch.setattr(S, "NEXUS_TOOL_EXECUTION_DEFAULT", "client_exec")
    req = request(tools=[ToolSpec(function=ToolFunction(name="client_tool", parameters={"type": "object", "properties": {}}))])
    policy = resolve_execution_policy(req, None)
    assert policy.mode == "client_exec"
    assert prepare_tools(req, policy, None) is req


def test_gateway_exec_rejects_tool_disabled_alias(monkeypatch):
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    req = request(x_nexus={"tool_execution_mode": "gateway_exec"})
    alias = ModelAlias(backend="local_vllm_fast", upstream_model="fast", tools=False)
    with pytest.raises(ValueError, match="does not support"):
        prepare_tools(req, resolve_execution_policy(req, alias), alias)


def test_disabled_mode_rejects_tool_fields():
    req = request(tool_choice="required", x_nexus={"tool_execution_mode": "disabled"})
    with pytest.raises(ValueError, match="disabled"):
        prepare_tools(req, resolve_execution_policy(req, None), None)


def test_gateway_exec_rejects_unapproved_named_tool(monkeypatch):
    monkeypatch.setattr(S, "NEXUS_AUTO_INJECT_TOOLS", True)
    req = request(
        tool_choice={"type": "function", "function": {"name": "client_delete_everything"}},
        x_nexus={"tool_execution_mode": "gateway_exec"},
    )
    alias = ModelAlias(backend="local_vllm", upstream_model="model", tools=True)
    with pytest.raises(ValueError, match="not approved"):
        prepare_tools(req, resolve_execution_policy(req, alias), alias)
