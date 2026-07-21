import json
import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.tool_calling.streaming import stream_final_chat_response


@pytest.mark.asyncio
async def test_gateway_exec_stream_buffers_loop_and_emits_valid_final_sse():
    response = {"id": "chatcmpl-final", "model": "model", "choices": [{"message": {"role": "assistant", "content": "final answer"}, "finish_reason": "stop"}]}
    chunks = [chunk async for chunk in stream_final_chat_response(response)]
    text = b"".join(chunks).decode()
    assert '"content":"final answer"' in text
    assert text.endswith("data: [DONE]\n\n")
    for block in text.split("\n\n"):
        if block.startswith("data: {"):
            json.loads(block[6:])


@pytest.mark.asyncio
async def test_buffered_stream_emits_openai_tool_call_deltas():
    response = {
        "id": "chatcmpl-tool",
        "model": "model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "demo", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    chunks = [chunk async for chunk in stream_final_chat_response(response)]
    text = b"".join(chunks).decode()

    assert '"tool_calls":[{"index":0,"id":"call_1"' in text
    assert '"name":"demo","arguments":"{}"' in text
    assert '"finish_reason":"tool_calls"' in text
    assert text.endswith("data: [DONE]\n\n")
