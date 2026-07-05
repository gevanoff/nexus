from __future__ import annotations

from app.openai_utils import sanitize_chat_choices


def test_sanitize_chat_choices_normalizes_nonstream_tool_call_shape():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "get_weather",
                                "arguments": {"city": "Paris", "unit": "celsius"},
                            }
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ]
    }

    out = sanitize_chat_choices(payload)
    tool_call = out["choices"][0]["message"]["tool_calls"][0]

    assert tool_call["id"].startswith("call-")
    assert tool_call["type"] == "function"
    assert tool_call["function"] == {
        "name": "get_weather",
        "arguments": '{"city":"Paris","unit":"celsius"}',
    }
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_sanitize_chat_choices_normalizes_stream_delta_tool_arguments_without_new_id():
    payload = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {
                                "name": "get_weather",
                                "arguments": {"city": "Paris"},
                            },
                        }
                    ]
                }
            }
        ]
    }

    out = sanitize_chat_choices(payload)
    tool_call = out["choices"][0]["delta"]["tool_calls"][0]

    assert tool_call["index"] == 0
    assert "id" not in tool_call
    assert tool_call["type"] == "function"
    assert tool_call["function"] == {
        "name": "get_weather",
        "arguments": '{"city":"Paris"}',
    }


def test_sanitize_chat_choices_suppresses_contaminated_nonstream_tool_name():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "grep_dirs</arg_value>pattern</arg_key><arg_value>stackrot</arg_value>",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    diagnostics = []

    out = sanitize_chat_choices(payload, allowed_tool_names={"grep_dirs"}, tool_diagnostics=diagnostics)
    choice = out["choices"][0]
    message = choice["message"]

    assert "tool_calls" not in message
    assert "suppressed an invalid backend tool call" in message["content"]
    assert choice["finish_reason"] == "stop"
    assert diagnostics[0]["reason"] == "malformed tool name"
    assert "grep_dirs" in diagnostics[0]["allowed_tool_names"]


def test_sanitize_chat_choices_suppresses_unknown_nonstream_tool_name():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "grep_dirs", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    diagnostics = []

    out = sanitize_chat_choices(payload, allowed_tool_names={"search_text"}, tool_diagnostics=diagnostics)

    assert "tool_calls" not in out["choices"][0]["message"]
    assert out["choices"][0]["finish_reason"] == "stop"
    assert diagnostics[0]["reason"] == "unknown tool name"
