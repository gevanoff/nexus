#!/usr/bin/env python3
import json
import os
import sys
from typing import Any

import httpx


def _write_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _error(detail: Any, *, http_status: int | None = None) -> int:
    payload: dict[str, Any] = {"status": "error", "detail": detail}
    if http_status is not None:
        payload["http_status"] = http_status
    _write_json(payload)
    return 1


def main() -> int:
    try:
        payload: dict[str, Any] = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError) as exc:
        return _error(f"invalid tool input: {type(exc).__name__}: {exc}")

    base_url = (os.environ.get("HUNYUAN_VIDEO_BASE_URL") or "http://127.0.0.1:9185").rstrip("/")
    timeout = float(os.environ.get("HUNYUAN_VIDEO_TIMEOUT_SEC") or 3600)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{base_url}/v1/videos/generations", json=payload)
    except httpx.HTTPError as exc:
        return _error(f"HunyuanVideo backend request failed: {type(exc).__name__}: {exc}")

    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[-4000:]
    if response.status_code >= 400:
        return _error(body, http_status=response.status_code)
    _write_json(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
