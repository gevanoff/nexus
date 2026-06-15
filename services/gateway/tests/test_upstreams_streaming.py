from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import upstreams


class _AsyncErrorStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error":{"message":"context too long"}}'


@pytest.mark.asyncio
async def test_stream_openai_chat_reports_unread_http_error_body(monkeypatch):
    captured = {}

    class Client:
        @asynccontextmanager
        async def stream(self, method, url, json, headers):
            captured["method"] = method
            captured["url"] = url
            response = httpx.Response(
                400,
                request=httpx.Request(method, url),
                stream=_AsyncErrorStream(),
            )
            yield response

    @asynccontextmanager
    async def fake_httpx_client(*, timeout=None):
        captured["timeout"] = timeout
        yield Client()

    monkeypatch.setattr(upstreams, "_httpx_client", fake_httpx_client)
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda _backend: "vllm")

    chunks = [
        chunk
        async for chunk in upstreams.stream_openai_chat(
            {
                "model": "fast-model",
                "stream": True,
                "messages": [{"role": "user", "content": "x"}],
            },
            base_url="http://backend.test/v1",
            backend_name="local_vllm_fast",
        )
    ]
    body = b"".join(chunks)

    assert captured["timeout"] is None
    assert captured["url"] == "http://backend.test/v1/chat/completions"
    assert b"upstream_error" in body
    assert b"context too long" in body
    assert chunks[-1] == b"data: [DONE]\n\n"
