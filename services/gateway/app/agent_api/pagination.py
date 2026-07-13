from __future__ import annotations

import base64
import json
from typing import Callable, Sequence, TypeVar

from app.agent_api.errors import ApiError


T = TypeVar("T")


def encode_cursor(timestamp: float, item_id: str) -> str:
    raw = json.dumps([float(timestamp), str(item_id)], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str]:
    value = str(cursor or "").strip()
    if not value:
        raise ApiError(400, "INVALID_CURSOR", "Cursor must not be empty")
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError("invalid cursor shape")
        return float(decoded[0]), str(decoded[1])
    except Exception as exc:
        raise ApiError(400, "INVALID_CURSOR", "Cursor is invalid") from exc


def paginate(
    items: Sequence[T],
    *,
    limit: int,
    cursor: str | None,
    timestamp: Callable[[T], float],
    item_id: Callable[[T], str],
) -> tuple[list[T], str | None]:
    ordered = sorted(items, key=lambda item: (timestamp(item), item_id(item)), reverse=True)
    if cursor:
        cursor_key = decode_cursor(cursor)
        ordered = [item for item in ordered if (timestamp(item), item_id(item)) < cursor_key]
    page = ordered[: limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(timestamp(last), item_id(last))
    return list(page), next_cursor
