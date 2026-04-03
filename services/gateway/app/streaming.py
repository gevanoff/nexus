from __future__ import annotations

import json
import asyncio
from typing import Any, AsyncIterator, Dict

import httpx

from app.openai_utils import ThinkTagStreamParser, new_id, now_unix, sanitize_chat_choices, sse, sse_done


async def passthrough_sse(resp: httpx.Response) -> AsyncIterator[bytes]:
    """
    Pass-through upstream SSE (already 'data: ...\n\n') from MLX-style OpenAI servers.
    """
    done_seen = False
    parser = ThinkTagStreamParser()
    try:
        async for line in resp.aiter_lines():
            if not line:
                continue

            if not line.startswith("data:"):
                continue

            data = line[len("data:") :].strip()
            if data == "[DONE]":
                done_seen = True
                tail_visible, tail_thinking = parser.flush()
                if tail_visible or tail_thinking:
                    delta: Dict[str, Any] = {}
                    if tail_visible:
                        delta["content"] = tail_visible
                    if tail_thinking:
                        delta["thinking"] = tail_thinking
                    if parser.drain_reset():
                        delta["thinking_reset"] = True
                    yield sse(
                        {
                            "id": new_id("chatcmpl"),
                            "object": "chat.completion.chunk",
                            "created": now_unix(),
                            "model": "",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    )
                yield sse_done()
                return

            try:
                obj = json.loads(data)
            except Exception:
                yield f"{line}\n\n".encode("utf-8")
                continue

            yield sse(sanitize_chat_choices(obj, stream_parser=parser))
    except asyncio.CancelledError:
        return

    # If upstream ends without a done marker, still end cleanly.
    tail_visible, tail_thinking = parser.flush()
    if tail_visible or tail_thinking:
        delta: Dict[str, Any] = {}
        if tail_visible:
            delta["content"] = tail_visible
        if tail_thinking:
            delta["thinking"] = tail_thinking
        if parser.drain_reset():
            delta["thinking_reset"] = True
        yield sse(
            {
                "id": new_id("chatcmpl"),
                "object": "chat.completion.chunk",
                "created": now_unix(),
                "model": "",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
    if not done_seen:
        yield sse_done()

