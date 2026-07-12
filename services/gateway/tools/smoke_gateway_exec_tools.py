#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request


def request(base_url: str, token: str, payload: dict, timeout: float) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, {key.lower(): value for key, value in exc.headers.items()}, exc.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Nexus Gateway-side tool execution")
    parser.add_argument("--base-url", default="http://127.0.0.1:8800/v1")
    parser.add_argument("--token", default=os.environ.get("GATEWAY_BEARER_TOKEN", ""))
    parser.add_argument("--models", default="default,coder,long")
    parser.add_argument("--toolsets", default="core,repo,ops")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or GATEWAY_BEARER_TOKEN is required")

    failed = 0
    for model in [item.strip() for item in args.models.split(",") if item.strip()]:
        for stream in (False, True):
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Use nexus_health, then summarize the result in one sentence."}],
                "tool_choice": {"type": "function", "function": {"name": "nexus_health"}},
                "stream": stream,
                "x_nexus": {
                    "tool_execution_mode": "gateway_exec",
                    "toolsets": [item.strip() for item in args.toolsets.split(",") if item.strip()],
                    "max_tool_rounds": 4,
                },
            }
            started = time.monotonic()
            status, headers, body = request(args.base_url, args.token, payload, args.timeout)
            executed = [item for item in headers.get("x-nexus-tools-executed", "").split(",") if item]
            final_seen = ("[DONE]" in body and "chat.completion.chunk" in body) if stream else bool((json.loads(body).get("choices") or [{}])[0].get("message", {}).get("content")) if status == 200 else False
            passed = status == 200 and "nexus_health" in executed and final_seen
            row = {
                "model": model,
                "mode": "gateway_exec",
                "scenario": "nexus_health_stream" if stream else "nexus_health",
                "http_status": status,
                "tool_calls_seen": executed,
                "tools_executed": executed,
                "final_answer_seen": final_seen,
                "passed": passed,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": None if passed else body[:500],
            }
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            failed += 0 if passed else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
