from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException


_FULL_READ_TIMEOUT_MARKERS = (
    "readtimeout:",
    "read timeout after",
)


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
