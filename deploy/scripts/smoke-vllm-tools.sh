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
cases = [part.strip() for part in (os.getenv("SMOKE_CASES") or "auto,required,named,none").split(",") if part.strip()]

if not model:
    print("MODEL or VLLM_SERVED_MODEL_NAME is required", file=sys.stderr)
    raise SystemExit(64)

tool = {
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
}


def request_body(case):
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "When a tool is needed, emit exactly one tool call and no prose.",
            },
            {"role": "user", "content": "Use the get_weather tool for Boston."},
        ],
        "tools": [tool],
        "temperature": 0,
        "max_tokens": int(os.getenv("MAX_TOKENS") or "256"),
    }
    if case == "auto":
        body["tool_choice"] = "auto"
    elif case == "required":
        body["tool_choice"] = "required"
    elif case == "named":
        body["tool_choice"] = {"type": "function", "function": {"name": "get_weather"}}
    elif case == "none":
        body["tool_choice"] = "none"
        body["messages"][-1]["content"] = "Answer normally without using tools: say hello in five words."
        body["max_tokens"] = min(body["max_tokens"], 64)
    else:
        print(f"Unknown SMOKE_CASES entry: {case}", file=sys.stderr)
        raise SystemExit(64)
    return body


def post_json(body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def tool_call_names(payload):
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    return [
        ((call.get("function") or {}).get("name") or "")
        for call in (tool_calls or [])
        if isinstance(call, dict)
    ]

for attempt in range(1, repeats + 1):
    for case in cases:
        body = request_body(case)
        try:
            status, text = post_json(body)
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"vLLM tool smoke failed case={case} attempt={attempt}: {type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(1)

        if status != 200:
            print(f"vLLM tool smoke failed case={case} attempt={attempt}: HTTP {status}", file=sys.stderr)
            print(text[:2000], file=sys.stderr)
            raise SystemExit(1)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"vLLM tool smoke failed case={case} attempt={attempt}: invalid JSON: {exc}", file=sys.stderr)
            print(text[:2000], file=sys.stderr)
            raise SystemExit(1)

        names = [name for name in tool_call_names(payload) if name]
        if case == "none":
            if names:
                print(f"vLLM tool smoke failed case={case} attempt={attempt}: unexpected tool_calls={','.join(names)}", file=sys.stderr)
                print(json.dumps(payload, indent=2)[:2000], file=sys.stderr)
                raise SystemExit(1)
            print(f"vLLM tool smoke passed case={case} attempt={attempt}/{repeats}: no tool calls")
            continue

        if "get_weather" not in names:
            print(f"vLLM tool smoke failed case={case} attempt={attempt}: get_weather tool_call not returned", file=sys.stderr)
            print(json.dumps(payload, indent=2)[:2000], file=sys.stderr)
            raise SystemExit(1)

        print(f"vLLM tool smoke passed case={case} attempt={attempt}/{repeats}: tool_calls=" + ",".join(names))
PY
