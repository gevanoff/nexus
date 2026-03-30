from __future__ import annotations

import json
import secrets
import time
from typing import Any


def now_unix() -> int:
    return int(time.time())


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def sse(data_obj: Any) -> bytes:
    return f"data: {json.dumps(data_obj, separators=(',', ':'))}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


class ThinkTagStreamParser:
    _START = "<think>"
    _END = "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._buffer = ""

    @classmethod
    def _partial_suffix_len(cls, text: str) -> int:
        lower = text.lower()
        best = 0
        for token in (cls._START.lower(), cls._END.lower()):
            max_len = min(len(lower), len(token) - 1)
            for size in range(max_len, 0, -1):
                if lower.endswith(token[:size]):
                    best = max(best, size)
                    break
        return best

    def feed(self, text: str) -> tuple[str, str]:
        if not isinstance(text, str) or not text:
            return "", ""

        self._buffer += text
        visible_parts: list[str] = []
        thinking_parts: list[str] = []

        while self._buffer:
            lower = self._buffer.lower()

            if self._inside:
                end_idx = lower.find(self._END)
                if end_idx == -1:
                    keep = min(len(self._buffer), len(self._END) - 1)
                    thought = self._buffer[:-keep] if keep else self._buffer
                    if thought:
                        thinking_parts.append(thought)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    break
                if end_idx > 0:
                    thinking_parts.append(self._buffer[:end_idx])
                self._buffer = self._buffer[end_idx + len(self._END) :]
                self._inside = False
                continue

            start_idx = lower.find(self._START)
            end_idx = lower.find(self._END)

            if end_idx != -1 and (start_idx == -1 or end_idx < start_idx):
                if end_idx > 0:
                    visible_parts.append(self._buffer[:end_idx])
                self._buffer = self._buffer[end_idx + len(self._END) :]
                continue

            if start_idx == -1:
                keep = self._partial_suffix_len(self._buffer)
                visible = self._buffer[:-keep] if keep else self._buffer
                if visible:
                    visible_parts.append(visible)
                self._buffer = self._buffer[-keep:] if keep else ""
                break

            if start_idx > 0:
                visible_parts.append(self._buffer[:start_idx])
            self._buffer = self._buffer[start_idx + len(self._START) :]
            self._inside = True

        return "".join(visible_parts), "".join(thinking_parts)

    def flush(self) -> tuple[str, str]:
        if self._inside:
            thought = self._buffer
            self._buffer = ""
            self._inside = False
            return "", thought
        tail = self._buffer.replace(self._START, "").replace(self._END, "")
        self._buffer = ""
        return tail, ""


def split_think_content(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or not text:
        return text, ""
    parser = ThinkTagStreamParser()
    visible, thinking = parser.feed(text)
    tail_visible, tail_thinking = parser.flush()
    return visible + tail_visible, thinking + tail_thinking


def sanitize_chat_choices(payload: Any, *, stream_parser: ThinkTagStreamParser | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                visible, thinking = stream_parser.feed(content) if stream_parser else split_think_content(content)
                delta["content"] = visible
                if thinking:
                    existing = delta.get("thinking")
                    if isinstance(existing, str) and existing:
                        delta["thinking"] = existing + thinking
                    else:
                        delta["thinking"] = thinking

        message = choice.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                visible, thinking = split_think_content(content)
                message["content"] = visible
                if thinking:
                    existing = message.get("thinking")
                    if isinstance(existing, str) and existing:
                        message["thinking"] = existing + thinking
                    else:
                        message["thinking"] = thinking

    return payload
