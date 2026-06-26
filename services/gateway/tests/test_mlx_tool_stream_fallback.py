from __future__ import annotations

from app import upstreams


def test_chat_completion_delta_from_message_includes_content_and_tool_calls():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
    ]

    delta = upstreams._chat_completion_delta_from_message(
        {
            "role": "assistant",
            "content": "hello",
            "tool_calls": tool_calls,
            "reasoning_content": "hidden",
        }
    )

    assert delta["content"] == "hello"
    assert delta["tool_calls"] == tool_calls
    assert delta["reasoning_content"] == "hidden"


def test_payload_has_tools_detects_nonempty_tools():
    assert upstreams._payload_has_tools({"tools": [{"type": "function", "function": {"name": "x"}}]})
    assert not upstreams._payload_has_tools({"tools": []})
    assert not upstreams._payload_has_tools({})
