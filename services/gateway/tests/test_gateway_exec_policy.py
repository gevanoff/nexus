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


def test_explicit_alias_tool_mode_overrides_server_default(monkeypatch):
    monkeypatch.setattr(S, "NEXUS_TOOL_EXECUTION_DEFAULT", "client_exec")
    alias = ModelAlias(
        backend="local_vllm",
        upstream_model="model",
        tools=True,
        tool_mode="gateway_exec",
        tool_mode_explicit=True,
    )

    assert resolve_execution_policy(request(), alias).mode == "gateway_exec"


def test_implicit_alias_tool_mode_uses_server_default(monkeypatch):
    monkeypatch.setattr(S, "NEXUS_TOOL_EXECUTION_DEFAULT", "gateway_exec")
    alias = ModelAlias(backend="local_vllm", upstream_model="model", tools=True)

    assert resolve_execution_policy(request(), alias).mode == "gateway_exec"


def test_request_cannot_raise_server_or_alias_execution_caps(monkeypatch):
    monkeypatch.setattr(S, "NEXUS_TOOL_MAX_ROUNDS", 4)
    monkeypatch.setattr(S, "NEXUS_TOOL_MAX_PARALLEL", 3)
    alias = ModelAlias(backend="local_vllm", upstream_model="model", tools=True, max_tool_rounds=2)

    policy = resolve_execution_policy(
        request(x_nexus={"tool_execution_mode": "gateway_exec", "max_tool_rounds": 12, "max_parallel_tools": 10}),
        alias,
    )

    assert policy.max_tool_rounds == 2
    assert policy.max_parallel_tools == 3
