from __future__ import annotations

import asyncio
import json

from app.streaming import passthrough_sse


class FakeSseResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def _collect(resp: FakeSseResponse) -> list[bytes]:
    return [chunk async for chunk in passthrough_sse(resp)]


def _payloads(chunks: list[bytes]) -> list[dict]:
    out: list[dict] = []
    for raw in chunks:
        text = raw.decode("utf-8")
        for block in text.strip().split("\n\n"):
            if not block.startswith("data: "):
                continue
            data = block[len("data: ") :]
            if data == "[DONE]":
                continue
            out.append(json.loads(data))
    return out


def _done_count(chunks: list[bytes]) -> int:
    return sum(chunk.count(b"data: [DONE]\n\n") for chunk in chunks)


def test_passthrough_sse_emits_visible_content_before_stop_chunk():
    resp = FakeSseResponse(
        [
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{"content":"LONG_STREAM_TOOL_OK"}}]}',
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
    )

    chunks = asyncio.run(_collect(resp))
    payloads = _payloads(chunks)

    content_indexes = [
        idx
        for idx, payload in enumerate(payloads)
        if payload["choices"][0].get("delta", {}).get("content") == "LONG_STREAM_TOOL_OK"
    ]
    finish_indexes = [
        idx
        for idx, payload in enumerate(payloads)
        if payload["choices"][0].get("finish_reason") == "stop"
    ]

    assert content_indexes
    assert finish_indexes
    assert max(content_indexes) < min(finish_indexes)
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert _done_count(chunks) == 1


def test_passthrough_sse_appends_done_after_terminal_tool_calls_without_upstream_done():
    tool_call_chunk = {
        "id": "upstream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mlx-test",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_readme",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"filepath":"README.md"}',
                            },
                        }
                    ],
                },
            }
        ],
    }
    terminal_chunk = {
        "id": "upstream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mlx-test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    resp = FakeSseResponse(
        [
            f"data: {json.dumps(tool_call_chunk)}",
            f"data: {json.dumps(terminal_chunk)}",
        ]
    )

    chunks = asyncio.run(_collect(resp))
    payloads = _payloads(chunks)

    assert any(
        payload["choices"][0].get("delta", {}).get("tool_calls") for payload in payloads
    )
    assert any(
        payload["choices"][0].get("finish_reason") == "tool_calls" for payload in payloads
    )
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert _done_count(chunks) == 1


def test_passthrough_sse_does_not_duplicate_upstream_done_after_terminal_stop():
    resp = FakeSseResponse(
        [
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    chunks = asyncio.run(_collect(resp))
    payloads = _payloads(chunks)

    assert any(
        payload["choices"][0].get("finish_reason") == "stop" for payload in payloads
    )
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert _done_count(chunks) == 1
