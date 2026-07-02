#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import ssl
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_MODELS = ["fast", "default", "coder"]
DEFAULT_SYSTEM_PROMPT = (
    "You are being used for a local model throughput benchmark. "
    "Return plain text only and continue until the output limit is reached."
)
DEFAULT_PROMPT = (
    "Generate a long plain-text benchmark response. Write short numbered sentences "
    "about operating local AI model infrastructure. Keep going until you reach the "
    "requested output limit. Do not summarize, ask questions, or stop early."
)
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4.0

_TLS_CONTEXT: ssl.SSLContext | None = None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env_first(*keys: str) -> str:
    for key in keys:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _env_flag(name: str) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _base_v1(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")]
    if value.endswith("/models"):
        value = value[: -len("/models")]
    if value.endswith("/v1"):
        return value
    return value + "/v1"


def _api_url(base_url: str, path: str) -> str:
    return _base_v1(base_url).rstrip("/") + "/" + path.lstrip("/")


def _auth_headers(token: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _interesting_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("x-backend-used", "x-model-used", "x-router-reason", "x-request-id"):
        value = headers.get(key)
        if value:
            out[key] = value
    return out


def _estimate_completion_tokens(text: str) -> int:
    chars_estimate = len(text) / TOKEN_ESTIMATE_CHARS_PER_TOKEN
    words_estimate = len(text.split()) * 1.3
    return max(1, int(round(max(chars_estimate, words_estimate))))


def _completion_token_count(usage: dict[str, Any], text: str) -> tuple[int, str]:
    value = usage.get("completion_tokens")
    if isinstance(value, int) and value > 0:
        return value, "usage"
    if isinstance(value, float) and value > 0:
        return int(value), "usage"
    return _estimate_completion_tokens(text), "estimated"


def _json_request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout_sec: float = 30.0,
) -> tuple[int, dict[str, str], Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    request = Request(url=url, data=data, headers=_auth_headers(token), method=method.upper())
    try:
        with urlopen(request, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
            if not body:
                return status, headers, None
            return status, headers, json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        headers = {k.lower(): v for k, v in getattr(exc, "headers", {}).items()}
        body = exc.read(8192).decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(body)
        except Exception:
            parsed = {"error": body}
        return int(exc.code), headers, parsed


def _list_models(base_url: str, token: str, timeout_sec: float) -> list[str]:
    status, _headers, payload = _json_request("GET", _api_url(base_url, "models"), token=token, timeout_sec=timeout_sec)
    if status != 200:
        raise RuntimeError(f"models request failed with status={status}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("models response did not contain a data array")
    models: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            models.append(item["id"].strip())
    return sorted(dict.fromkeys(models))


def _extract_delta_text(choice: dict[str, Any]) -> str:
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def _parse_sse_payload(line: bytes | str) -> Any:
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace")
    else:
        text = line
    text = text.strip()
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if payload == "[DONE]":
        return "[DONE]"
    if not payload:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _chat_payload(args: argparse.Namespace, model: str, *, stream: bool) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if args.system_prompt:
        messages.append({"role": "system", "content": args.system_prompt})
    messages.append({"role": "user", "content": args.prompt})

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}

    optional_values = {
        "top_k": args.top_k,
        "min_p": args.min_p,
        "repetition_penalty": args.repetition_penalty,
        "seed": args.seed,
    }
    for key, value in optional_values.items():
        if value is not None:
            body[key] = value
    if args.stop:
        body["stop"] = args.stop
    if args.extra_json:
        extra = json.loads(args.extra_json)
        if not isinstance(extra, dict):
            raise ValueError("--extra-json must decode to a JSON object")
        body.update(extra)
    return body


def _chat_once(
    args: argparse.Namespace,
    *,
    model: str,
    phase: str,
    run_index: int,
    run_id: str,
) -> dict[str, Any]:
    stream = not bool(args.no_stream)
    payload = _chat_payload(args, model, stream=stream)
    url = _api_url(args.base_url, "chat/completions")
    request = Request(
        url=url,
        data=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        headers=_auth_headers(args.api_key),
        method="POST",
    )

    started = time.monotonic()
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason = ""
    ttft_ms: float | None = None
    status = 0
    response_headers: dict[str, str] = {}

    try:
        with urlopen(request, timeout=args.timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            response_headers = {k.lower(): v for k, v in resp.headers.items()}
            if stream:
                for raw_line in resp:
                    event = _parse_sse_payload(raw_line)
                    if event == "[DONE]":
                        break
                    if not isinstance(event, dict):
                        continue
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    piece = _extract_delta_text(choice)
                    if piece:
                        if ttft_ms is None:
                            ttft_ms = round((time.monotonic() - started) * 1000.0, 1)
                        text_parts.append(piece)
                    if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
                        finish_reason = choice["finish_reason"]
            else:
                body = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                if isinstance(parsed.get("usage"), dict):
                    usage = parsed["usage"]
                choices = parsed.get("choices")
                if isinstance(choices, list) and choices:
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                    content = message.get("content") or choice.get("text") or ""
                    text_parts.append(str(content))
                    if isinstance(choice.get("finish_reason"), str):
                        finish_reason = choice["finish_reason"]

        wall_ms = round((time.monotonic() - started) * 1000.0, 1)
        text = "".join(text_parts)
        completion_tokens, token_source = _completion_token_count(usage, text)
        wall_seconds = max(wall_ms / 1000.0, 0.001)
        decode_tokens_per_sec: float | None = None
        if ttft_ms is not None and wall_ms > ttft_ms:
            decode_tokens_per_sec = round(completion_tokens / max((wall_ms - ttft_ms) / 1000.0, 0.001), 2)

        result = {
            "schema": "nexus.model_benchmark.v1",
            "timestamp": _now_iso(),
            "run_id": run_id,
            "phase": phase,
            "run_index": run_index,
            "model": model,
            "ok": status == 200,
            "status": status,
            "base_url": _base_v1(args.base_url),
            "stream": stream,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "completion_tokens": completion_tokens,
            "completion_tokens_source": token_source,
            "prompt_chars": len(args.prompt),
            "response_chars": len(text),
            "wall_ms": wall_ms,
            "time_to_first_token_ms": ttft_ms,
            "tokens_per_sec": round(completion_tokens / wall_seconds, 2),
            "decode_tokens_per_sec": decode_tokens_per_sec,
            "chars_per_sec": round(len(text) / wall_seconds, 2),
            "finish_reason": finish_reason,
            "usage": usage,
            "headers": _interesting_headers(response_headers),
        }
        if args.keep_responses:
            result["response"] = text[: args.max_response_chars]
        return result
    except HTTPError as exc:
        error_body = exc.read(8192).decode("utf-8", errors="replace")
        return _error_result(args, model, phase, run_index, run_id, int(exc.code), started, error_body)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return _error_result(args, model, phase, run_index, run_id, 0, started, f"{type(exc).__name__}: {exc}")


def _error_result(
    args: argparse.Namespace,
    model: str,
    phase: str,
    run_index: int,
    run_id: str,
    status: int,
    started: float,
    error: str,
) -> dict[str, Any]:
    return {
        "schema": "nexus.model_benchmark.v1",
        "timestamp": _now_iso(),
        "run_id": run_id,
        "phase": phase,
        "run_index": run_index,
        "model": model,
        "ok": False,
        "status": status,
        "base_url": _base_v1(args.base_url),
        "stream": not bool(args.no_stream),
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
        "error": str(error)[:4000],
    }


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(item, separators=(",", ":"), sort_keys=True))
        handle.write("\n")


def _expand_models(values: Iterable[str]) -> list[str]:
    models: list[str] = []
    for value in values:
        for part in str(value).split(","):
            model = part.strip()
            if model:
                models.append(model)
    return list(dict.fromkeys(models))


def _summarize(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_model: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if result.get("phase") != "measure":
            continue
        by_model.setdefault(str(result.get("model") or ""), []).append(result)

    for model, model_results in by_model.items():
        ok_results = [item for item in model_results if item.get("ok")]
        errors = [str(item.get("error") or f"status={item.get('status')}") for item in model_results if not item.get("ok")]
        tps_values = [float(item["tokens_per_sec"]) for item in ok_results if isinstance(item.get("tokens_per_sec"), (int, float))]
        decode_values = [float(item["decode_tokens_per_sec"]) for item in ok_results if isinstance(item.get("decode_tokens_per_sec"), (int, float))]
        ttft_values = [float(item["time_to_first_token_ms"]) for item in ok_results if isinstance(item.get("time_to_first_token_ms"), (int, float))]
        token_values = [int(item["completion_tokens"]) for item in ok_results if isinstance(item.get("completion_tokens"), int)]
        headers = next((item.get("headers") for item in ok_results if isinstance(item.get("headers"), dict) and item.get("headers")), {})
        rows.append(
            {
                "model": model,
                "runs": len(model_results),
                "ok": len(ok_results),
                "tokens_per_sec_avg": round(statistics.mean(tps_values), 2) if tps_values else None,
                "tokens_per_sec_min": round(min(tps_values), 2) if tps_values else None,
                "decode_tokens_per_sec_avg": round(statistics.mean(decode_values), 2) if decode_values else None,
                "ttft_ms_avg": round(statistics.mean(ttft_values), 1) if ttft_values else None,
                "completion_tokens_avg": round(statistics.mean(token_values), 1) if token_values else None,
                "backend": headers.get("x-backend-used") if isinstance(headers, dict) else None,
                "resolved_model": headers.get("x-model-used") if isinstance(headers, dict) else None,
                "error": "; ".join(errors[:2]),
            }
        )
    return rows


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _print_table(rows: list[dict[str, Any]]) -> None:
    columns = [
        ("model", "model"),
        ("ok", "ok"),
        ("runs", "runs"),
        ("tokens_per_sec_avg", "tok/s avg"),
        ("tokens_per_sec_min", "tok/s min"),
        ("decode_tokens_per_sec_avg", "decode tok/s"),
        ("ttft_ms_avg", "ttft ms"),
        ("completion_tokens_avg", "tokens"),
        ("backend", "backend"),
        ("resolved_model", "resolved model"),
        ("error", "error"),
    ]
    widths = []
    for key, label in columns:
        width = len(label)
        for row in rows:
            width = max(width, len(_format_value(row.get(key))))
        widths.append(min(width, 64))

    header = "  ".join(label.ljust(widths[idx]) for idx, (_key, label) in enumerate(columns))
    print(header)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        cells = []
        for idx, (key, _label) in enumerate(columns):
            value = _format_value(row.get(key))
            if len(value) > widths[idx]:
                value = value[: max(0, widths[idx] - 1)] + "…"
            cells.append(value.ljust(widths[idx]))
        print("  ".join(cells))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    runtime_root = os.getenv("NEXUS_RUNTIME_ROOT", ".runtime")
    parser = argparse.ArgumentParser(description="Benchmark OpenAI-compatible Nexus chat models for tokens/sec.")
    parser.add_argument("--base-url", default=_env_first("GATEWAY_OPENAI_BASE_URL", "OPENAI_BASE_URL") or "http://127.0.0.1:8800/v1")
    parser.add_argument("--api-key", default=_env_first("GATEWAY_BEARER_TOKEN", "OPENAI_API_KEY"))
    parser.add_argument("--model", action="append", default=[], help="Model or alias to benchmark. May be repeated or comma-separated.")
    parser.add_argument("--all-models", action="store_true", help="Benchmark every id returned by /v1/models.")
    parser.add_argument("--list-models", action="store_true", help="List /v1/models ids and exit.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout-sec", type=float, default=300.0)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--min-p", type=float, default=None)
    parser.add_argument("--repetition-penalty", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--stop", action="append", default=[])
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--prompt-file", default="", help="Read the user prompt from this file.")
    parser.add_argument("--extra-json", default="", help="Merge this JSON object into each chat request body.")
    parser.add_argument("--no-stream", action="store_true", help="Use non-streaming chat completions. TTFT and decode tok/s will be unavailable.")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for self-signed local HTTPS.")
    parser.add_argument("--out", default=os.path.join(runtime_root, "model-benchmarks", "results.jsonl"))
    parser.add_argument("--json", action="store_true", help="Print summary JSON instead of a table.")
    parser.add_argument("--keep-responses", action="store_true")
    parser.add_argument("--max-response-chars", type=int, default=20_000)
    args = parser.parse_args(argv)

    if args.prompt_file:
        args.prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.warmup_runs < 0:
        parser.error("--warmup-runs must be non-negative")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    global _TLS_CONTEXT

    args = parse_args(argv)
    if args.insecure or _env_flag("GATEWAY_TLS_INSECURE"):
        _TLS_CONTEXT = ssl._create_unverified_context()

    if not args.api_key:
        print("ERROR: missing API key. Set GATEWAY_BEARER_TOKEN or OPENAI_API_KEY, or pass --api-key.", file=sys.stderr)
        return 2

    try:
        if args.list_models:
            for model in _list_models(args.base_url, args.api_key, args.timeout_sec):
                print(model)
            return 0

        models = _expand_models(args.model)
        if args.all_models:
            models = _list_models(args.base_url, args.api_key, args.timeout_sec)
        if not models:
            env_models = _expand_models([os.getenv("NEXUS_BENCH_MODELS", "")])
            models = env_models or list(DEFAULT_MODELS)
    except Exception as exc:
        print(f"ERROR: failed to resolve models: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    out_path = Path(args.out).resolve()
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    all_results: list[dict[str, Any]] = []

    for model in models:
        for run_index in range(1, args.warmup_runs + 1):
            result = _chat_once(args, model=model, phase="warmup", run_index=run_index, run_id=run_id)
            _append_jsonl(out_path, result)
            all_results.append(result)
            status = "ok" if result.get("ok") else "fail"
            print(f"warmup {status} model={model} run={run_index} wall_ms={result.get('wall_ms')}", file=sys.stderr)

        for run_index in range(1, args.runs + 1):
            result = _chat_once(args, model=model, phase="measure", run_index=run_index, run_id=run_id)
            _append_jsonl(out_path, result)
            all_results.append(result)
            if result.get("ok"):
                print(
                    f"measure ok model={model} run={run_index} "
                    f"tokens={result.get('completion_tokens')} tok/s={result.get('tokens_per_sec')} "
                    f"ttft_ms={result.get('time_to_first_token_ms')}",
                    file=sys.stderr,
                )
            else:
                print(f"measure fail model={model} run={run_index} error={result.get('error')}", file=sys.stderr)

    summary = _summarize(all_results)
    if args.json:
        print(json.dumps({"run_id": run_id, "out": str(out_path), "summary": summary}, indent=2, sort_keys=True))
    else:
        _print_table(summary)
        print(f"\nwrote JSONL results to {out_path}")

    return 0 if all(row.get("ok") == row.get("runs") for row in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
