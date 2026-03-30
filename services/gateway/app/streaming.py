from __future__ import annotations

import json
import asyncio
from typing import Any, AsyncIterator, Dict

import httpx

from app.config import logger
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


async def ollama_ndjson_to_openai_sse(
    resp: httpx.Response,
    *,
    model_name: str,
    chunk_id: str | None = None,
    created: int | None = None,
    emit_role_chunk: bool = True,
) -> AsyncIterator[bytes]:
    """
    Translate Ollama NDJSON streaming into OpenAI SSE chat.completion.chunk events.
    """
    chunk_id = chunk_id or new_id("chatcmpl")
    created = created or now_unix()

    sent_role = not emit_role_chunk
    content_emitted = False
    parser = ThinkTagStreamParser()
    if emit_role_chunk:
        # First chunk: announce assistant role (common expectation)
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        sent_role = True

    try:
        async for line in resp.aiter_lines():
            if not line:
                continue

            try:
                obj = json.loads(line)
            except Exception:
                continue

            # Ollama may return a non-NDJSON JSON error payload even when called with
            # stream=true. Surface this as an OpenAI-style error event.
            err = obj.get("error") if isinstance(obj, dict) else None
            if isinstance(err, str) and err:
                logger.warning("ollama stream error model=%s error=%r", model_name, err)
                yield sse(
                    {
                        "error": {
                            "message": err,
                            "type": "upstream_error",
                            "param": None,
                            "code": None,
                            "detail": {"upstream": "ollama", "model": model_name},
                        }
                    }
                )
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                yield sse_done()
                return

            # Ollama /api/chat uses "message": {"role":"assistant","content":"..."} and "done"
            # /api/generate uses "response": "..." and "done"
            done = bool(obj.get("done", False))

            # Prefer chat field
            content = None
            msg = obj.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                thinking = msg.get("thinking") or msg.get("reasoning") or msg.get("thoughts")
            else:
                thinking = None

            # Fallback to generate field
            if content is None:
                content = obj.get("response")

            if isinstance(thinking, str) and thinking:
                explicit_thinking = thinking
            else:
                explicit_thinking = ""
            if content:
                visible, tag_thinking = parser.feed(content)
            else:
                visible, tag_thinking = "", ""

            combined_thinking = explicit_thinking + tag_thinking
            if combined_thinking:
                delta: Dict[str, Any] = {"thinking": combined_thinking}
                if not sent_role:
                    delta["role"] = "assistant"
                    sent_role = True
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                )

            if visible:
                content_emitted = True
                delta = {"content": visible}
                if not sent_role:
                    delta["role"] = "assistant"
                    sent_role = True
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                )

            if done:
                finish_reason = obj.get("done_reason") or "stop"
                tail_visible, tail_thinking = parser.flush()
                if tail_thinking:
                    yield sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"thinking": tail_thinking}, "finish_reason": None}],
                        }
                    )
                if tail_visible:
                    content_emitted = True
                    delta: Dict[str, Any] = {"content": tail_visible}
                    if parser.drain_reset():
                        delta["thinking_reset"] = True
                    yield sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    )
                if not content_emitted:
                    # Useful for diagnosing alias models that immediately end without output.
                    logger.warning(
                        "ollama stream ended with no content model=%s done_reason=%r keys=%s",
                        model_name,
                        obj.get("done_reason"),
                        sorted(list(obj.keys())),
                    )
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    }
                )
                yield sse_done()
                return

    except asyncio.CancelledError:
        return

    # If upstream ends without a done marker, still end cleanly.
    tail_visible, tail_thinking = parser.flush()
    if tail_thinking:
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"thinking": tail_thinking}, "finish_reason": None}],
            }
        )
    if tail_visible:
        delta: Dict[str, Any] = {"content": tail_visible}
        if parser.drain_reset():
            delta["thinking_reset"] = True
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    yield sse_done()
