from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import DefaultDict


_lock = threading.Lock()

# request metrics
_req_count: DefaultDict[tuple[str, int], int] = defaultdict(int)
_req_dur_ms_sum: DefaultDict[tuple[str, int], float] = defaultdict(float)

# tool metrics
_tool_count: DefaultDict[tuple[str, str], int] = defaultdict(int)  # (tool, status)
_tool_runtime_ms_sum: DefaultDict[tuple[str, str], float] = defaultdict(float)

# coarse buckets in ms
_TOOL_BUCKETS_MS = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
_tool_runtime_ms_bucket: DefaultDict[tuple[str, str, int], int] = defaultdict(int)  # (tool, status, le)


def observe_request(path: str, status: int, duration_ms: float) -> None:
    if not path:
        return
    try:
        st = int(status)
    except Exception:
        st = 0
    try:
        dur = float(duration_ms)
    except Exception:
        dur = 0.0
    with _lock:
        _req_count[(path, st)] += 1
        _req_dur_ms_sum[(path, st)] += max(0.0, dur)


def observe_tool(tool: str, ok: bool, runtime_ms: float) -> None:
    if not tool:
        return
    status = "ok" if ok else "error"
    try:
        dur = float(runtime_ms)
    except Exception:
        dur = 0.0
    with _lock:
        _tool_count[(tool, status)] += 1
        _tool_runtime_ms_sum[(tool, status)] += max(0.0, dur)
        for le in _TOOL_BUCKETS_MS:
            if dur <= le:
                _tool_runtime_ms_bucket[(tool, status, le)] += 1
        _tool_runtime_ms_bucket[(tool, status, 10_000_000)] += 1  # +Inf


def _escape_label(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\"", "\\\"")
    )


def render_prometheus_text() -> str:
    # Minimal Prometheus exposition; no dependencies.
    now = int(time.time())
    lines: list[str] = []
    lines.append(f"# gateway_metrics_generated {now}")

    lines.append("# TYPE gateway_requests_total counter")
    with _lock:
        for (path, status), n in sorted(_req_count.items()):
            lines.append(
                f'gateway_requests_total{{path="{_escape_label(path)}",status="{status}"}} {n}'
            )

        lines.append("# TYPE gateway_request_duration_ms_sum counter")
        for (path, status), s in sorted(_req_dur_ms_sum.items()):
            lines.append(
                f'gateway_request_duration_ms_sum{{path="{_escape_label(path)}",status="{status}"}} {s:.1f}'
            )

        lines.append("# TYPE gateway_tool_invocations_total counter")
        for (tool, status), n in sorted(_tool_count.items()):
            lines.append(
                f'gateway_tool_invocations_total{{tool="{_escape_label(tool)}",status="{status}"}} {n}'
            )

        lines.append("# TYPE gateway_tool_runtime_ms_sum counter")
        for (tool, status), s in sorted(_tool_runtime_ms_sum.items()):
            lines.append(
                f'gateway_tool_runtime_ms_sum{{tool="{_escape_label(tool)}",status="{status}"}} {s:.1f}'
            )

        lines.append("# TYPE gateway_tool_runtime_ms_bucket counter")
        for (tool, status, le), n in sorted(_tool_runtime_ms_bucket.items()):
            le_label = "+Inf" if le == 10_000_000 else str(le)
            lines.append(
                f'gateway_tool_runtime_ms_bucket{{tool="{_escape_label(tool)}",status="{status}",le="{le_label}"}} {n}'
            )

    return "\n".join(lines) + "\n"
