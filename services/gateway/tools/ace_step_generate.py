#!/usr/bin/env python3
import json
import os
import sys
from typing import Any

import httpx


def main() -> int:
    payload: dict[str, Any] = json.load(sys.stdin)
    base_url = (os.environ.get("ACE_STEP_BASE_URL") or "http://127.0.0.1:9195").rstrip("/")
    timeout = float(os.environ.get("ACE_STEP_TIMEOUT_SEC") or 1800)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"{base_url}/v1/music/generations", json=payload)
    sys.stdout.write(response.text)
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
