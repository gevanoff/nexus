from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import pytest
from fastapi import HTTPException

from app.models import ChatCompletionRequest
from app import upstreams
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



def test_normalize_messages_handles_continue_camelcase_and_text_parts():
    tool_calls = [
        {
            "id": "call_123",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
    ]

    normalized = _normalize_messages_for_openai_backend(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "input_text", "text": "second"},
                ],
            },
            {"role": "assistant", "content": "", "toolCalls": tool_calls},
            {"role": "tool", "toolCallId": "call_123", "content": "done"},
        ]
    )

    assert normalized[0] == {"role": "user", "content": "first\nsecond"}
    assert normalized[1] == {"role": "assistant", "content": "", "tool_calls": tool_calls}
    assert normalized[2] == {"role": "tool", "content": "done", "tool_call_id": "call_123"}


def test_glm_input_guard_rejects_oversized_prompt(monkeypatch):
    monkeypatch.setattr(upstreams.S, "MLX_GLM_MAX_INPUT_CHARS", 100, raising=False)
    request = ChatCompletionRequest(
        model="coder",
        messages=[{"role": "user", "content": "x" * 200}],
    )

    with pytest.raises(HTTPException) as exc_info:
        upstreams._enforce_mlx_glm_input_limit(
            request,
            backend_name="local_mlx",
            model_name="mlx-community/GLM-5.2-DQ4plus-q8",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "mlx_glm_input_too_large"
    assert exc_info.value.detail["input_chars"] > exc_info.value.detail["max_input_chars"]


def test_glm_input_guard_does_not_limit_other_models(monkeypatch):
    monkeypatch.setattr(upstreams.S, "MLX_GLM_MAX_INPUT_CHARS", 100, raising=False)
    request = ChatCompletionRequest(
        model="phi",
        messages=[{"role": "user", "content": "x" * 200}],
    )

    upstreams._enforce_mlx_glm_input_limit(
        request,
        backend_name="local_mlx",
        model_name="mlx-community/Phi-4-reasoning-plus-4bit",
    )
