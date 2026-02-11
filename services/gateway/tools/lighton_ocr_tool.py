#!/usr/bin/env python3
import json
import os
import sys
from typing import Any, Dict

import httpx


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _base_url() -> str:
    return _env("LIGHTON_OCR_API_BASE_URL", "http://127.0.0.1:9155").rstrip("/")


def main() -> int:
    payload: Dict[str, Any] = json.load(sys.stdin)
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(f"{_base_url()}/v1/ocr", json=payload)
        if resp.status_code >= 400:
            sys.stdout.write(json.dumps({"error": resp.text, "status": resp.status_code}))
            return 1
        sys.stdout.write(resp.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
