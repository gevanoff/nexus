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
    scenarios = [
        ("nexus_health", "core", {"include_upstreams": False, "include_models": False}),
        ("nexus_file_read", "repo", {"path": "README.md", "start_line": 1, "end_line": 8, "max_chars": 2000}),
        ("nexus_resources_snapshot", "ops", {"scope": "local"}),
    ]
    selected_toolsets = [item.strip() for item in args.toolsets.split(",") if item.strip()]
    for model in [item.strip() for item in args.models.split(",") if item.strip()]:
        runs = [(name, toolset, arguments, False) for name, toolset, arguments in scenarios if toolset in selected_toolsets]
        if "core" in selected_toolsets:
            runs.append(("nexus_health", "core", scenarios[0][2], True))
        for tool_name, toolset, tool_arguments, stream in runs:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": f"Use {tool_name}, then summarize the result in one sentence."}],
                "tool_choice": {"type": "function", "function": {"name": tool_name}},
                "stream": stream,
                "x_nexus": {
                    "tool_execution_mode": "gateway_exec",
                    "toolsets": selected_toolsets,
                    "max_tool_rounds": 4,
                },
            }
            started = time.monotonic()
            status, headers, body = request(args.base_url, args.token, payload, args.timeout)
            executed = [item for item in headers.get("x-nexus-tools-executed", "").split(",") if item]
            final_seen = ("[DONE]" in body and "chat.completion.chunk" in body) if stream else bool((json.loads(body).get("choices") or [{}])[0].get("message", {}).get("content")) if status == 200 else False
            passed = status == 200 and tool_name in executed and final_seen
            row = {
                "model": model,
                "mode": "gateway_exec",
                "scenario": f"{tool_name}_stream" if stream else tool_name,
                "toolset": toolset,
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
