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
cases = [
    part.strip()
    for part in (os.getenv("SMOKE_CASES") or "auto,required,named,none,parallel,roundtrip").split(",")
    if part.strip()
]

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
    elif case == "parallel":
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
        body["messages"][-1]["content"] = (
            "Call get_weather exactly twice in parallel: once for Boston and once for Tokyo. "
            "Return exactly two tool calls and no prose."
        )
    elif case == "roundtrip":
        body["tool_choice"] = "auto"
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


def response_message(payload):
    choices = payload.get("choices") or []
    return choices[0].get("message") if choices and isinstance(choices[0], dict) else {}


def tool_calls(payload):
    message = response_message(payload)
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    return [call for call in (tool_calls or []) if isinstance(call, dict)]


def tool_call_names(payload):
    return [((call.get("function") or {}).get("name") or "") for call in tool_calls(payload)]


def parse_arguments(call):
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def run_roundtrip(first_payload):
    calls = tool_calls(first_payload)
    if not calls:
        return False, "initial tool call not returned", first_payload
    normalized_calls = []
    for index, call in enumerate(calls, start=1):
        normalized = dict(call)
        normalized["id"] = call.get("id") or f"call_smoke_{index}"
        normalized_calls.append(normalized)
    assistant = response_message(first_payload)
    messages = [
        {"role": "system", "content": "Use tool results, then follow the user's exact response instruction."},
        {"role": "user", "content": "Use the get_weather tool for Boston."},
        {
            "role": "assistant",
            "content": assistant.get("content") or "",
            "tool_calls": normalized_calls,
        },
    ]
    for call in normalized_calls:
        call_id = call["id"]
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"weather": "TOOL_ROUNDTRIP_OK", "city": "Boston"}),
            }
        )
    messages.append({"role": "user", "content": "Reply with exactly TOOL_ROUNDTRIP_OK."})
    followup = {
        "model": model,
        "messages": messages,
        "tools": [tool],
        "tool_choice": "none",
        "temperature": 0,
        "max_tokens": 64,
    }
    status, text = post_json(followup)
    if status != 200:
        return False, f"follow-up HTTP {status}", text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"follow-up invalid JSON: {exc}", text
    content = response_message(payload).get("content") or ""
    if "TOOL_ROUNDTRIP_OK" not in content:
        return False, "follow-up did not preserve the tool result", payload
    if tool_calls(payload):
        return False, "follow-up returned an unexpected tool call", payload
    return True, "roundtrip completed", payload

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

        if case == "parallel":
            calls = [call for call in tool_calls(payload) if ((call.get("function") or {}).get("name") == "get_weather")]
            cities = {str(parse_arguments(call).get("city") or "").lower() for call in calls}
            if len(calls) < 2 or not any("boston" in city for city in cities) or not any("tokyo" in city for city in cities):
                print(
                    f"vLLM tool smoke failed case={case} attempt={attempt}: expected Boston and Tokyo tool calls",
                    file=sys.stderr,
                )
                print(json.dumps(payload, indent=2)[:2000], file=sys.stderr)
                raise SystemExit(1)
            print(f"vLLM tool smoke passed case={case} attempt={attempt}/{repeats}: parallel calls={len(calls)}")
            continue

        if "get_weather" not in names:
            print(f"vLLM tool smoke failed case={case} attempt={attempt}: get_weather tool_call not returned", file=sys.stderr)
            print(json.dumps(payload, indent=2)[:2000], file=sys.stderr)
            raise SystemExit(1)

        if case == "roundtrip":
            try:
                passed, detail, roundtrip_payload = run_roundtrip(payload)
            except urllib.error.HTTPError as exc:
                passed = False
                detail = f"follow-up HTTP {exc.code}"
                roundtrip_payload = exc.read().decode("utf-8", errors="replace")
            except Exception as exc:
                passed = False
                detail = f"follow-up {type(exc).__name__}: {exc}"
                roundtrip_payload = ""
            if not passed:
                print(f"vLLM tool smoke failed case={case} attempt={attempt}: {detail}", file=sys.stderr)
                rendered = roundtrip_payload if isinstance(roundtrip_payload, str) else json.dumps(roundtrip_payload, indent=2)
                print(rendered[:2000], file=sys.stderr)
                raise SystemExit(1)
            print(f"vLLM tool smoke passed case={case} attempt={attempt}/{repeats}: {detail}")
            continue

        print(f"vLLM tool smoke passed case={case} attempt={attempt}/{repeats}: tool_calls=" + ",".join(names))
PY
