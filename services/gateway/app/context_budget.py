from __future__ import annotations

import json
import math
from typing import Any, Sequence


TOKEN_ESTIMATOR_NAME = "nexus_conservative_chars_v1"
_ASCII_CHARS_PER_TOKEN = 3.0
_NON_ASCII_CHARS_PER_TOKEN = 1.5


def estimate_text_tokens(text: str) -> int:
    """Return a conservative dependency-free token estimate.

    Gateway cannot assume the huge model tokenizer is present in its own
    process. This estimator deliberately budgets code and JSON more
    conservatively than the common four-characters-per-token heuristic while
    charging non-ASCII text more heavily. Alias limits retain explicit safety
    headroom around this estimate.
    """

    value = str(text or "")
    if not value:
        return 0
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    non_ascii_chars = len(value) - ascii_chars
    estimate = (
        ascii_chars / _ASCII_CHARS_PER_TOKEN
        + non_ascii_chars / _NON_ASCII_CHARS_PER_TOKEN
    )
    return max(1, int(math.ceil(estimate)))


def estimate_tokens(value: Any) -> int:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        serialized = str(value or "")
    return estimate_text_tokens(serialized)


def estimate_char_budget_tokens(char_count: int) -> int:
    try:
        count = max(0, int(char_count))
    except Exception:
        count = 0
    if count <= 0:
        return 0
    return max(1, int(math.ceil(count / _ASCII_CHARS_PER_TOKEN)))


def estimate_chat_tokens(messages: Sequence[Any], *, tools: Any = None) -> int:
    payload: dict[str, Any] = {"messages": []}
    for message in messages:
        if hasattr(message, "model_dump"):
            try:
                payload["messages"].append(message.model_dump(exclude_none=True))
                continue
            except Exception:
                pass
        payload["messages"].append(message)
    if tools is not None:
        payload["tools"] = [
            item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item
            for item in tools
        ]
    return estimate_tokens(payload)
