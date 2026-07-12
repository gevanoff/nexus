import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.model_aliases import ModelAlias
from app.tool_calling.capabilities import capability_for_alias


def test_alias_capability_diagnostics_include_provider_parser_and_policy():
    alias = ModelAlias(
        backend="local_vllm",
        upstream_model="model",
        tools=True,
        supports_tool_choice=("none", "auto", "required", "named"),
        supports_parallel_tool_calls=True,
        preferred_tool_call_parser="xlam",
        preferred_chat_template="tool-use.jinja",
        toolsets=("core", "repo"),
    )
    result = capability_for_alias("default", alias)
    assert result["tools_enabled"] is True
    assert result["tool_call_parser"] == "xlam"
    assert result["parallel_tool_calls"] is True
    assert result["toolsets"] == ["core", "repo"]
