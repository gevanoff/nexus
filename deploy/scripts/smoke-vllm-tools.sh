#!/usr/bin/env sh
set -eu

python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base_url = (os.getenv("BASE_URL") or os.getenv("VLLM_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
model = os.getenv("MODEL") or os.getenv("VLLM_SERVED_MODEL_NAME") or os.getenv("VLLM_MODEL_STRONG") or ""
api_key = os.getenv("API_KEY") or ""
repeats = int(os.getenv("REPEATS") or "1")

if not model:
    print("MODEL or VLLM_SERVED_MODEL_NAME is required", file=sys.stderr)
    raise SystemExit(64)

body = {
    "model": model,
    "messages": [
        {
            "role": "system",
            "content": "When a tool is needed, emit exactly one tool call and no prose.",
        },
        {"role": "user", "content": "Use the get_weather tool for Boston."},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Return weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                    },
                    "required": ["city"],
                },
            },
        },
    ],
    "tool_choice": "auto",
    "temperature": 0,
    "max_tokens": int(os.getenv("MAX_TOKENS") or "256"),
}

for attempt in range(1, repeats + 1):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        text = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"vLLM tool smoke failed on attempt {attempt}: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if status != 200:
        print(f"vLLM tool smoke failed on attempt {attempt}: HTTP {status}", file=sys.stderr)
        print(text[:2000], file=sys.stderr)
        raise SystemExit(1)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"vLLM tool smoke failed on attempt {attempt}: invalid JSON: {exc}", file=sys.stderr)
        print(text[:2000], file=sys.stderr)
        raise SystemExit(1)

    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not tool_calls:
        print(f"vLLM tool smoke failed on attempt {attempt}: no tool_calls returned", file=sys.stderr)
        print(json.dumps(payload, indent=2)[:2000], file=sys.stderr)
        raise SystemExit(1)

    names = [
        ((call.get("function") or {}).get("name") or "")
        for call in tool_calls
        if isinstance(call, dict)
    ]
    print(f"vLLM tool smoke passed attempt {attempt}/{repeats}: tool_calls=" + ",".join(name for name in names if name))
PY
