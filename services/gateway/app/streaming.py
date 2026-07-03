from __future__ import annotations

import json
import asyncio
from typing import Any, AsyncIterator, Dict

import httpx

from app.openai_utils import ThinkTagStreamParser, new_id, now_unix, sanitize_chat_choices, sse, sse_done


def _append_text_field(target: Dict[str, Any], field: str, text: str) -> None:
    existing = target.get(field)
    if isinstance(existing, str) and existing:
        target[field] = existing + text
    else:
        target[field] = text


def _tail_delta(visible: str, hidden: str) -> Dict[str, Any]:
    delta: Dict[str, Any] = {}
    if visible:
        delta["content"] = visible
    if hidden:
        for field in ("thinking", "reasoning_content"):
            _append_text_field(delta, field, hidden)
    return delta


def _tail_chunk(visible: str, hidden: str, *, model: str = "") -> bytes | None:
    delta = _tail_delta(visible, hidden)
    if not delta:
        return None
    return sse(
        {
            "id": new_id("chatcmpl"),
            "object": "chat.completion.chunk",
            "created": now_unix(),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
    )


def _has_terminal_choice(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, dict) and choice.get("finish_reason") is not None:
            return True
    return False


def _normalize_stream_error_payload(payload: Any, *, request_id: str | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload
    error = payload.get("error")
    if not isinstance(error, dict):
        return payload
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return payload

    normalized = dict(payload)
    normalized_error = dict(error)
    req_id = request_id or "-"
    normalized_error["message"] = f"Upstream returned an empty streaming error; request_id={req_id}"
    normalized_error.setdefault("type", "server_error")
    normalized_error.setdefault("param", None)
    normalized_error.setdefault("code", "500")
    normalized["error"] = normalized_error
    return normalized


async def passthrough_sse(resp: httpx.Response, *, request_id: str | None = None) -> AsyncIterator[bytes]:
    """
    Normalize upstream OpenAI-style SSE while preserving explicit reasoning side
    channels and strict OpenAI chunk ordering.
    """
    done_seen = False
    terminal_seen = False
    last_model = ""
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
                if not terminal_seen:
                    tail_visible, tail_hidden = parser.flush()
                    tail = _tail_chunk(tail_visible, tail_hidden, model=last_model)
                    if tail is not None:
                        yield tail
                yield sse_done()
                return

            try:
                obj = json.loads(data)
            except Exception:
                yield f"{line}\n\n".encode("utf-8")
                continue

            obj = _normalize_stream_error_payload(obj, request_id=request_id)

            if isinstance(obj, dict):
                model = obj.get("model")
                if isinstance(model, str) and model:
                    last_model = model

            if isinstance(obj, dict) and str(obj.get("type") or "") == "response.output_text.delta":
                visible = str(obj.get("delta") or "")
                delta: Dict[str, Any] = {}
                if visible:
                    delta["content"] = visible
                if delta:
                    yield sse(
                        {
                            "id": new_id("chatcmpl"),
                            "object": "chat.completion.chunk",
                            "created": now_unix(),
                            "model": last_model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    )
                continue

            sanitized = sanitize_chat_choices(obj, stream_parser=parser)
            if _has_terminal_choice(sanitized):
                terminal_seen = True
                tail_visible, tail_hidden = parser.flush()
                tail = _tail_chunk(tail_visible, tail_hidden, model=last_model)
                if tail is not None:
                    yield tail
            yield sse(sanitized)
    except asyncio.CancelledError:
        return

    # If upstream ends without a done marker, still end cleanly. Do not emit
    # additional content after a terminal finish_reason chunk has been sent.
    if not terminal_seen:
        tail_visible, tail_hidden = parser.flush()
        tail = _tail_chunk(tail_visible, tail_hidden, model=last_model)
        if tail is not None:
            yield tail
    if not done_seen:
        yield sse_done()
