from __future__ import annotations

import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import tool_loop as tl
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec


@pytest.mark.asyncio
async def test_tool_loop_uses_auto_after_required_tool_result(monkeypatch):
    seen_tool_choices = []

    async def fake_call(req, _backend: str, _model_name: str):
        seen_tool_choices.append(req.tool_choice)
        if len(seen_tool_choices) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "noop", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(tl, "call_backend_chat", fake_call)
    monkeypatch.setattr(tl, "run_tool_call", lambda *_args, **_kwargs: {"ok": True})

    req = ChatCompletionRequest(
        model="qualified",
        messages=[ChatMessage(role="user", content="use a tool")],
        tools=[ToolSpec(function=ToolFunction(name="noop", parameters={"type": "object", "properties": {}}))],
        tool_choice="required",
        stream=False,
    )

    resp = await tl.tool_loop(req, "local_vllm", "upstream-model")

    assert resp["choices"][0]["message"]["content"] == "done"
    assert seen_tool_choices == ["required", "auto"]
