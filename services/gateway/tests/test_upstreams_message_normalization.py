from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.upstreams import _normalize_messages_for_openai_backend


def test_normalize_messages_preserves_tool_call_exchange():
    tool_calls = [
        {
            "id": "call_123",
            "type": "function",
            "function": {"name": "coding_list_tree", "arguments": "{}"},
        }
    ]

    normalized = _normalize_messages_for_openai_backend(
        [
            {"role": "user", "content": "inspect the repo"},
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
            {"role": "tool", "tool_call_id": "call_123", "content": {"ok": True}},
        ]
    )

    assert normalized[1] == {"role": "assistant", "content": "", "tool_calls": tool_calls}
    assert normalized[2] == {"role": "tool", "content": {"ok": True}, "tool_call_id": "call_123"}


def test_normalize_messages_only_merges_plain_text_neighbors():
    normalized = _normalize_messages_for_openai_backend(
        [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
            {"role": "tool", "tool_call_id": "call_123", "content": "tool result"},
            {"role": "tool", "tool_call_id": "call_456", "content": "other result"},
        ]
    )

    assert normalized == [
        {"role": "user", "content": "first\nsecond"},
        {"role": "tool", "content": "tool result", "tool_call_id": "call_123"},
        {"role": "tool", "content": "other result", "tool_call_id": "call_456"},
    ]
