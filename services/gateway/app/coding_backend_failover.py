from __future__ import annotations

import json
import math
import time
from typing import Any, Mapping

from fastapi import HTTPException


_FULL_READ_TIMEOUT_MARKERS = (
    "readtimeout:",
    "read timeout after",
)

COOLDOWNS_KEY = "coding_backend_cooldowns"
COOLDOWN_SEC = 1800.0
MAX_COOLDOWN_SEC = 7200.0


def route_key(backend: str, upstream_model: str) -> str:
    """An unambiguous identity; aliases sharing a backend need not share a model."""
    return json.dumps([backend.strip(), upstream_model.strip()], separators=(",", ":"))


def cooldown_state(task: Mapping[str, Any], *, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    raw = task.get(COOLDOWNS_KEY)
    if not isinstance(raw, Mapping):
        return []
    records = []
    for key, value in sorted(raw.items()):
        if not isinstance(value, Mapping):
            continue
        record = dict(value)
        try:
            retry_after = float(record.get("retry_after") or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(retry_after):
            continue
        record.update(route_key=key, active=retry_after > now)
        records.append(record)
    return records


def filter_task_candidates(
    candidates: list[Mapping[str, Any]], task: Mapping[str, Any], *, now: float | None = None,
) -> list[Mapping[str, Any]]:
    excluded = {item["route_key"] for item in cooldown_state(task, now=now) if item["active"]}
    return [item for item in candidates if route_key(
        str(item.get("backend") or ""), str(item.get("upstream_model") or "")
    ) not in excluded]


def record_full_timeout(cw: Any, task_id: str, backend: str, upstream_model: str) -> None:
    """Persist before testing retry budget, including the last failed attempt."""
    now = time.time()
    key = route_key(backend, upstream_model)

    def apply(task: dict[str, Any]) -> None:
        records = dict(task.get(COOLDOWNS_KEY) or {})
        previous = records.get(key) or {}
        failures = min(8, int(previous.get("failures") or 0) + 1)
        records[key] = {
            "backend": backend, "upstream_model": upstream_model,
            "reason": "full_generation_read_timeout", "failures": failures,
            "first_failure_at": previous.get("first_failure_at") or now,
            "last_failure_at": now,
            "retry_after": now + min(MAX_COOLDOWN_SEC, COOLDOWN_SEC * 2 ** (failures - 1)),
            "last_failure_run_id": str(task.get("agent_run_id") or ""),
        }
        task[COOLDOWNS_KEY] = records

    cw.mutate_task(task_id, apply)


def record_success(
    cw: Any, task_id: str, backend: str, upstream_model: str, *, started_at: float | None = None,
) -> None:
    key = route_key(backend, upstream_model)
    if key not in (cw.load_task(task_id).get(COOLDOWNS_KEY) or {}):
        return

    def apply(task: dict[str, Any]) -> None:
        records = dict(task.get(COOLDOWNS_KEY) or {})
        record = dict(records.get(key) or {})
        if record and (started_at is None or float(record.get("last_failure_at") or 0) <= started_at):
            record.update(failures=0, retry_after=0.0, recovered_at=time.time())
            records[key] = record
            task[COOLDOWNS_KEY] = records

    cw.mutate_task(task_id, apply)


def backend_error_detail(exc: HTTPException) -> dict[str, Any]:
    if isinstance(exc.detail, dict):
        return dict(exc.detail)
    return {"error": str(exc.detail)}


def is_full_generation_read_timeout(exc: HTTPException) -> bool:
    """Return true only for an upstream generation read timeout.

    Connect failures and ordinary transient 5xx responses remain eligible for
    normal same-backend retries. Once generation has already consumed a full
    read window, retrying the same backend simply grants another full window;
    the coding router should instead exclude that backend for this request and
    attempt another healthy coding route.
    """
    detail = backend_error_detail(exc)
    text = " ".join(
        str(detail.get(key) or "")
        for key in ("error", "body", "message")
    ).strip().lower()
    if not text:
        return False
    return all(marker in text for marker in _FULL_READ_TIMEOUT_MARKERS)


def retry_exclusions_after_error(
    excluded_backends: set[str],
    *,
    backend: str,
    exc: HTTPException,
) -> set[str]:
    updated = set(excluded_backends)
    if is_full_generation_read_timeout(exc):
        normalized = str(backend or "").strip()
        if normalized:
            updated.add(normalized)
    return updated


def filter_candidates(
    candidates: list[Mapping[str, Any]],
    excluded_backends: set[str] | None = None,
) -> list[Mapping[str, Any]]:
    excluded = {str(item).strip() for item in (excluded_backends or set()) if str(item).strip()}
    if not excluded:
        return list(candidates)
    return [
        item
        for item in candidates
        if str(item.get("backend") or "").strip() not in excluded
    ]
