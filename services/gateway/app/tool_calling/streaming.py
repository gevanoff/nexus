from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.openai_utils import new_id, now_unix, sse, sse_done


async def stream_final_chat_response(response: dict[str, Any]) -> AsyncIterator[bytes]:
    choice = ((response.get("choices") or [{}])[0] or {}) if isinstance(response, dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    model = str(response.get("model") or "") if isinstance(response, dict) else ""
    response_id = str(response.get("id") or new_id("chatcmpl")) if isinstance(response, dict) else new_id("chatcmpl")
    base = {"id": response_id, "object": "chat.completion.chunk", "created": now_unix(), "model": model}
    yield sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    content = (message or {}).get("content")
    if isinstance(content, str) and content:
        yield sse({**base, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
    yield sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]})
    yield sse_done()
