from __future__ import annotations

import json
import asyncio
from contextlib import suppress
import logging
from typing import Any, AsyncIterator, Dict

import httpx

from app.openai_utils import ToolCallValidationState, ThinkTagStreamParser, new_id, now_unix, sanitize_chat_choices, sse, sse_done


log = logging.getLogger("uvicorn.error")


async def with_sse_heartbeat(
    source: AsyncIterator[bytes],
    *,
    interval_sec: float = 15.0,
    immediate: bool = True,
) -> AsyncIterator[bytes]:
    """Keep an SSE response alive without cancelling a slow upstream read."""
    interval = max(0.1, float(interval_sec or 15.0))
    iterator = source.__aiter__()
    pending: asyncio.Future[bytes] | None = None
    heartbeat = b": nexus-keepalive\n\n"
    try:
        if immediate:
            yield heartbeat
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield heartbeat
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield item
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await close()


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


def _downgrade_invalid_tool_finish(obj: Any, state: ToolCallValidationState) -> None:
    if state.invalid_count <= 0 or state.valid_count > 0:
        return
    if not isinstance(obj, dict):
        return
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if isinstance(choice, dict) and choice.get("finish_reason") == "tool_calls":
            choice["finish_reason"] = "stop"


def _log_invalid_tool_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    request_id: str | None,
    backend_name: str,
    model_name: str,
) -> None:
    if not diagnostics:
        return
    for item in diagnostics[:5]:
        log.warning(
            "openai stream suppressed invalid backend tool call request_id=%s backend=%s model=%s reason=%s name=%r stream_index=%s allowed_tool_count=%s allowed_tools=%s",
            request_id or "-",
            backend_name or "-",
            model_name or "-",
            item.get("reason") or "",
            item.get("name") or "",
            item.get("stream_index"),
            item.get("allowed_tool_count"),
            item.get("allowed_tool_names"),
        )


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


async def passthrough_sse(
    resp: httpx.Response,
    *,
    request_id: str | None = None,
    allowed_tool_names: Any = None,
    tool_specs: Any = None,
    backend_name: str = "",
    model_name: str = "",
) -> AsyncIterator[bytes]:
    """
    Normalize upstream OpenAI-style SSE while preserving explicit reasoning side
    channels and strict OpenAI chunk ordering.
    """
    done_seen = False
    terminal_seen = False
    last_model = ""
    parser = ThinkTagStreamParser()
    tool_state = ToolCallValidationState(allowed_tool_names)
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

            diagnostics: list[dict[str, Any]] = []
            sanitized = sanitize_chat_choices(
                obj,
                stream_parser=parser,
                allowed_tool_names=allowed_tool_names,
                tool_specs=tool_specs,
                tool_diagnostics=diagnostics,
                stream_tool_state=tool_state,
            )
            _downgrade_invalid_tool_finish(sanitized, tool_state)
            _log_invalid_tool_diagnostics(
                diagnostics,
                request_id=request_id,
                backend_name=backend_name,
                model_name=model_name or last_model,
            )
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
