from __future__ import annotations

import json

import pytest

from app.streaming import passthrough_sse


class FakeSseResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


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


@pytest.mark.asyncio
async def test_passthrough_sse_emits_visible_content_before_stop_chunk():
    resp = FakeSseResponse(
        [
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{"content":"LONG_STREAM_TOOL_OK"}}]}',
            'data: {"id":"upstream","object":"chat.completion.chunk","created":1,"model":"mlx-test","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
    )

    chunks = [chunk async for chunk in passthrough_sse(resp)]
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
