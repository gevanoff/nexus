#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark GLM-5.2 latency and prompt-prefix reuse behavior.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1", help="OpenAI-compatible base URL")
    parser.add_argument("--model", default="glm-5.2", help="Model alias/id to benchmark")
    parser.add_argument("--api-key", default=os.getenv("NEXUS_API_KEY", ""), help="Bearer API key")
    parser.add_argument("--timeout-sec", type=float, default=120.0, help="HTTP timeout in seconds")
    parser.add_argument("--max-tokens", type=int, default=256, help="Max completion tokens")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    parser.add_argument("--top-p", type=float, default=1.0, help="Top-p")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming mode")
    return parser


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _prefix_hash(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] | None) -> Tuple[str, int]:
    prefix_messages = list(messages)
    if prefix_messages and str(prefix_messages[-1].get("role") or "").strip().lower() == "user":
        prefix_messages = prefix_messages[:-1]
    payload = {
        "messages": prefix_messages,
        "tools": tools or [],
    }
    serialized = _canonical_json(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), len(serialized)


def _sse_event(raw_line: bytes) -> Any:
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "[DONE]"
    if not payload:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _extract_content_delta(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    choices = event.get("choices")
    if not isinstance(choices, list):
        return ""
    total = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            total.append(delta.get("content") or "")
    return "".join(total)


def _extract_usage(event: Any) -> Dict[str, Any]:
    if isinstance(event, dict) and isinstance(event.get("usage"), dict):
        return event.get("usage")
    return {}


def _chat_once(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    timeout_sec: float,
    max_tokens: int,
    temperature: float,
    top_p: float,
    stream: bool,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    request_body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream,
    }
    if stream:
        request_body["stream_options"] = {"include_usage": True}

    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    request = Request(
        url=url,
        method="POST",
        headers=headers,
        data=_canonical_json(request_body).encode("utf-8"),
    )

    started = time.monotonic()
    ttft_ms: float | None = None
    usage: Dict[str, Any] = {}
    output_text = ""

    try:
        with urlopen(request, timeout=timeout_sec) as response:
            status = int(getattr(response, "status", response.getcode()))
            if stream:
                for raw_line in response:
                    event = _sse_event(raw_line)
                    if event == "[DONE]":
                        break
                    if event is None:
                        continue
                    usage = _extract_usage(event) or usage
                    delta = _extract_content_delta(event)
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = round((time.monotonic() - started) * 1000.0, 1)
                        output_text += delta
            else:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
                if choices and isinstance(choices[0], dict):
                    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
                    output_text = str(message.get("content") or choices[0].get("text") or "")

        total_ms = round((time.monotonic() - started) * 1000.0, 1)
        output_tokens = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), (int, float)) else None
        if output_tokens is None:
            output_tokens = max(1, int(round(len(output_text) / 4.0)))
        decode_tokens_per_sec = None
        if output_tokens is not None and total_ms > 0:
            if ttft_ms is not None and total_ms > ttft_ms:
                window_sec = max((total_ms - ttft_ms) / 1000.0, 0.001)
            else:
                window_sec = max(total_ms / 1000.0, 0.001)
            decode_tokens_per_sec = round(float(output_tokens) / window_sec, 2)

        return {
            "ok": status == 200,
            "status": status,
            "time_to_first_token_ms": ttft_ms,
            "total_ms": total_ms,
            "output_tokens": int(output_tokens),
            "decode_tokens_per_sec": decode_tokens_per_sec,
            "usage": usage,
            "output_text": output_text,
        }
    except HTTPError as exc:
        body = exc.read(4000).decode("utf-8", errors="replace")
        return {"ok": False, "status": int(exc.code), "error": body}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}"}


def _print_case(result: Dict[str, Any]) -> None:
    print(_canonical_json(result))


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    stream = not bool(args.no_stream)

    stable_system = (
        "You are a coding assistant in a benchmark run. Keep answers concise and deterministic. "
        "Do not include dates, random ids, or environment-specific data."
    )
    stable_context = (
        "Repository context: gevanoff/nexus gateway latency optimization. "
        "Prioritize low TTFT and quick actionable output for coding loops."
    )

    prefix_seen: Dict[str, int] = {}

    case_1_messages = [
        {"role": "system", "content": stable_system},
        {"role": "developer", "content": stable_context},
        {
            "role": "user",
            "content": (
                "Case 1: cold long prompt. Provide a 14-step checklist for reducing gateway TTFT while preserving OpenAI compatibility."
            ),
        },
    ]

    case_2_messages = [
        {"role": "system", "content": stable_system},
        {"role": "developer", "content": stable_context},
        {
            "role": "user",
            "content": (
                "Case 2: same prefix, different final instruction. Provide a 10-step checklist with a stronger focus on prompt-prefix determinism."
            ),
        },
    ]

    cases = [
        ("cold_long_prompt", case_1_messages),
        ("same_prefix_new_user", case_2_messages),
    ]

    results: List[Dict[str, Any]] = []
    previous_assistant = ""

    for case_name, messages in cases:
        prefix_hash, prefix_chars = _prefix_hash(messages, None)
        seen_before = prefix_hash in prefix_seen
        prefix_seen[prefix_hash] = prefix_seen.get(prefix_hash, 0) + 1

        run = _chat_once(
            base_url=base_url,
            api_key=args.api_key,
            model=args.model,
            messages=messages,
            timeout_sec=args.timeout_sec,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            stream=stream,
        )
        previous_assistant = run.get("output_text") if run.get("ok") else previous_assistant

        item = {
            "case": case_name,
            "model": args.model,
            "prompt_prefix_hash": prefix_hash,
            "prompt_prefix_chars": prefix_chars,
            "prefix_seen_before": seen_before,
            "approximate_prompt_chars": sum(len(str(msg.get("content") or "")) for msg in messages),
            **{k: v for k, v in run.items() if k != "output_text"},
        }
        results.append(item)
        _print_case(item)

    case_3_messages = [
        {"role": "system", "content": stable_system},
        {"role": "developer", "content": stable_context},
        {
            "role": "user",
            "content": (
                "Case 2: same prefix, different final instruction. Provide a 10-step checklist with a stronger focus on prompt-prefix determinism."
            ),
        },
        {"role": "assistant", "content": previous_assistant or "Acknowledged."},
        {
            "role": "user",
            "content": "Case 3: continuation follow-up. Condense your previous answer into the top 4 highest-impact changes.",
        },
    ]
    prefix_hash, prefix_chars = _prefix_hash(case_3_messages, None)
    seen_before = prefix_hash in prefix_seen
    prefix_seen[prefix_hash] = prefix_seen.get(prefix_hash, 0) + 1
    run3 = _chat_once(
        base_url=base_url,
        api_key=args.api_key,
        model=args.model,
        messages=case_3_messages,
        timeout_sec=args.timeout_sec,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        stream=stream,
    )
    item3 = {
        "case": "continuation_follow_up",
        "model": args.model,
        "prompt_prefix_hash": prefix_hash,
        "prompt_prefix_chars": prefix_chars,
        "prefix_seen_before": seen_before,
        "approximate_prompt_chars": sum(len(str(msg.get("content") or "")) for msg in case_3_messages),
        **{k: v for k, v in run3.items() if k != "output_text"},
    }
    results.append(item3)
    _print_case(item3)

    failed = [row for row in results if not row.get("ok")]
    if failed:
        print(_canonical_json({"summary": "failed", "failed_cases": [row.get("case") for row in failed]}), file=sys.stderr)
        return 1

    print(_canonical_json({"summary": "ok", "cases": [row.get("case") for row in results]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
