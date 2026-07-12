import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.tool_calling.registry import builtin_tool_definitions
from app.tool_calling.schemas import normalize_tool_definition


def test_all_builtin_nexus_tools_are_strict_compatible():
    for definition in builtin_tool_definitions().values():
        tool = normalize_tool_definition(definition.as_openai(), mode="strict_preserve")
        function = tool["function"]
        parameters = function["parameters"]
        assert function["strict"] is True
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])


def test_strict_autofix_converts_optional_property_to_required_nullable():
    tool = normalize_tool_definition(
        {"type": "function", "function": {"name": "demo", "parameters": {"type": "object", "properties": {"optional": {"type": "string"}}, "required": []}}},
        mode="strict_autofix",
    )
    parameters = tool["function"]["parameters"]
    assert parameters["required"] == ["optional"]
    assert parameters["properties"]["optional"]["type"] == ["string", "null"]
