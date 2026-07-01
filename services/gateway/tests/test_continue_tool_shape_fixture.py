from __future__ import annotations

from app import upstreams


def test_continue_style_tool_metadata_is_not_forwarded_upstream():
    payload = {
        "tools": [
            {
                "type": "function",
                "displayTitle": "Tool title",
                "wouldLikeTo": "use tool",
                "isCurrently": "using tool",
                "hasAlready": "used tool",
                "readonly": True,
                "isInstant": True,
                "group": "Built-In",
                "function": {
                    "name": "example_tool",
                    "description": "Example.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
                "systemMessageDescription": {
                    "prefix": "Example prefix",
                    "exampleArgs": [["value", "x"]],
                },
            }
        ]
    }

    normalized = upstreams._normalize_openai_tools_payload(payload)

    assert set(normalized["tools"][0]) == {"type", "function"}
    assert set(normalized["tools"][0]["function"]) == {"name", "description", "parameters"}
    assert normalized["tools"][0]["function"]["name"] == "example_tool"
