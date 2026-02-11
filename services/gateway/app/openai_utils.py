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
