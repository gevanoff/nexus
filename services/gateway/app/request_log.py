from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from app.config import S


def _truncate(v: Any, *, max_chars: int) -> Any:
    if isinstance(v, str) and len(v) > max_chars:
        return v[:max_chars] + "…"
    return v


def write_request_event(event: Dict[str, Any]) -> None:
    """Best-effort JSONL request logging.

    This must never raise; logging should not impact request handling.
    """

    if not getattr(S, "REQUEST_LOG_ENABLED", True):
        return

    path = (getattr(S, "REQUEST_LOG_PATH", "") or "/var/lib/gateway/data/requests.jsonl").strip()
    if not path:
        return

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        event = {
            k: _truncate(v, max_chars=20_000)
            for k, v in event.items()
            if isinstance(k, str) and k
        }
        line = json.dumps(event, separators=(",", ":"), sort_keys=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        return


class StreamMetrics:
    def __init__(self, *, started_monotonic: float):
        self.started_monotonic = started_monotonic
        self.first_chunk_monotonic: Optional[float] = None
        self.chunks: int = 0
        self.bytes: int = 0
        self.abort_reason: Optional[str] = None

    def on_chunk(self, chunk: bytes) -> None:
        if self.first_chunk_monotonic is None:
            self.first_chunk_monotonic = time.monotonic()
        self.chunks += 1
        self.bytes += len(chunk)

    def finish(self) -> Dict[str, Any]:
        done = time.monotonic()
        out: Dict[str, Any] = {
            "stream": True,
            "duration_ms": round((done - self.started_monotonic) * 1000.0, 1),
            "chunks_out": self.chunks,
            "bytes_out": self.bytes,
        }
        if self.first_chunk_monotonic is not None:
            out["ttft_ms"] = round((self.first_chunk_monotonic - self.started_monotonic) * 1000.0, 1)
        if self.abort_reason:
            out["abort_reason"] = self.abort_reason
        return out
