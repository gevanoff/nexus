from __future__ import annotations

import asyncio
import json
import statistics
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from app.backends import check_capability, get_admission_controller, get_registry
from app.config import S
from app.health_checker import check_backend_ready
from app.models import ChatCompletionRequest, ChatMessage
from app.openai_utils import now_unix
from app.router import decide_route
from app.router_cfg import router_cfg
from app.upstreams import call_backend_chat, stream_backend_chat_as_openai


DEFAULT_SYSTEM_PROMPT = (
    "You are being used for a local model throughput benchmark. "
    "Return plain text only and continue until the output limit is reached."
)
DEFAULT_PROMPT = (
    "Generate a long plain-text benchmark response. Write short numbered sentences "
    "about operating local AI model infrastructure. Keep going until you reach the "
    "requested output limit. Do not summarize, ask questions, or stop early."
)

MAX_MODELS_PER_RUN = 12
MAX_RUNS_PER_MODEL = 10
MAX_WARMUP_RUNS_PER_MODEL = 3
MAX_BENCHMARK_TOKENS = 4096
MAX_PROMPT_CHARS = 20_000
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4.0
RECENT_HISTORY_LINES = 2500

_BENCHMARK_LOCK = asyncio.Lock()


class BenchmarkBusy(RuntimeError):
    pass


class ModelBenchmarkRequest(BaseModel):
    models: List[str]
    runs: int = 3
    warmup_runs: int = 1
    max_tokens: int = 512
    temperature: float = 0.2
    top_p: float = 0.95
    stream: bool = True
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    prompt: str = DEFAULT_PROMPT


def benchmark_log_path() -> Path:
    value = (getattr(S, "MODEL_BENCHMARK_LOG_PATH", "") or "").strip()
    if value:
        return Path(value)
    return Path("/var/lib/gateway/data/model_benchmarks/results.jsonl")


def clean_model_list(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in str(value or "").split(","):
            item = part.strip()
            if item and item not in out:
                out.append(item)
    return out


def validate_request(req: ModelBenchmarkRequest) -> ModelBenchmarkRequest:
    models = clean_model_list(req.models)
    if not models:
        raise ValueError("at least one model is required")
    if len(models) > MAX_MODELS_PER_RUN:
        raise ValueError(f"at most {MAX_MODELS_PER_RUN} models can be benchmarked at once")
    if req.runs < 1 or req.runs > MAX_RUNS_PER_MODEL:
        raise ValueError(f"runs must be between 1 and {MAX_RUNS_PER_MODEL}")
    if req.warmup_runs < 0 or req.warmup_runs > MAX_WARMUP_RUNS_PER_MODEL:
        raise ValueError(f"warmup_runs must be between 0 and {MAX_WARMUP_RUNS_PER_MODEL}")
    if req.max_tokens < 1 or req.max_tokens > MAX_BENCHMARK_TOKENS:
        raise ValueError(f"max_tokens must be between 1 and {MAX_BENCHMARK_TOKENS}")
    if req.temperature < 0 or req.temperature > 2:
        raise ValueError("temperature must be between 0 and 2")
    if req.top_p <= 0 or req.top_p > 1:
        raise ValueError("top_p must be greater than 0 and at most 1")
    if len(req.prompt or "") > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be {MAX_PROMPT_CHARS} characters or less")
    if len(req.system_prompt or "") > MAX_PROMPT_CHARS:
        raise ValueError(f"system_prompt must be {MAX_PROMPT_CHARS} characters or less")
    return req.model_copy(update={"models": models})


def parse_sse_payload(line: bytes | str) -> Any:
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


def iter_sse_payloads(chunk: bytes | str) -> Iterable[Any]:
    if isinstance(chunk, str):
        lines = chunk.splitlines()
    else:
        lines = chunk.splitlines()
    for line in lines:
        event = parse_sse_payload(line)
        if event is not None:
            yield event


def extract_delta_text(choice: Dict[str, Any]) -> str:
    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def estimate_completion_tokens(text: str) -> int:
    if not text:
        return 0
    chars_estimate = len(text) / TOKEN_ESTIMATE_CHARS_PER_TOKEN
    words_estimate = len(text.split()) * 1.3
    return max(1, int(round(max(chars_estimate, words_estimate))))


def completion_token_count(usage: Dict[str, Any], text: str) -> tuple[int, str]:
    value = usage.get("completion_tokens")
    if isinstance(value, int) and value >= 0:
        return value, "usage"
    if isinstance(value, float) and value >= 0:
        return int(value), "usage"
    return estimate_completion_tokens(text), "estimated"


def error_text(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        try:
            return json.dumps(detail, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            return str(detail)
    return f"{type(exc).__name__}: {exc}"


def build_chat_request(req: ModelBenchmarkRequest, model: str) -> ChatCompletionRequest:
    messages = [
        ChatMessage(role="system", content=req.system_prompt or DEFAULT_SYSTEM_PROMPT),
        ChatMessage(role="user", content=req.prompt or DEFAULT_PROMPT),
    ]
    return ChatCompletionRequest(
        model=model,
        messages=messages,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        top_p=req.top_p,
        stream=req.stream,
        stream_options={"include_usage": True} if req.stream else None,
    )


def _route_chat_request(cc: ChatCompletionRequest) -> tuple[Any, str, str]:
    route = decide_route(
        cfg=router_cfg(),
        request_model=cc.model,
        headers={},
        messages=[m.model_dump(exclude_none=True) for m in cc.messages],
        has_tools=False,
        enable_policy=S.ROUTER_ENABLE_POLICY,
        enable_request_type=getattr(S, "ROUTER_ENABLE_REQUEST_TYPE", False),
    )
    registry = get_registry()
    backend_class = registry.resolve_backend_class(route.backend) or route.backend
    return route, backend_class, route.model


async def run_once(
    req: ModelBenchmarkRequest,
    *,
    model: str,
    phase: str,
    run_index: int,
    run_id: str,
) -> Dict[str, Any]:
    cc = build_chat_request(req, model)
    started = time.monotonic()
    created_at = now_unix()
    usage: Dict[str, Any] = {}
    text_parts: list[str] = []
    finish_reason = ""
    ttft_ms: float | None = None
    route_reason = ""
    backend = ""
    backend_class = ""
    upstream_model = ""
    stream_error = ""
    admission_acquired = False
    admission = get_admission_controller()

    try:
        route, backend_class, upstream_model = _route_chat_request(cc)
        backend = route.backend
        route_reason = route.reason

        check_backend_ready(backend_class, route_kind="chat")
        await check_capability(backend_class, "chat")
        await admission.acquire(backend_class, "chat")
        admission_acquired = True

        if req.stream:
            upstream_gen = stream_backend_chat_as_openai(cc, backend, upstream_model)
            stream_done = False
            async for chunk in upstream_gen:
                for event in iter_sse_payloads(chunk):
                    if event == "[DONE]":
                        stream_done = True
                        break
                    if not isinstance(event, dict):
                        continue
                    if isinstance(event.get("error"), dict):
                        stream_error = json.dumps(event["error"], ensure_ascii=False, separators=(",", ":"), default=str)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    piece = extract_delta_text(choice)
                    if piece:
                        if ttft_ms is None:
                            ttft_ms = round((time.monotonic() - started) * 1000.0, 1)
                        text_parts.append(piece)
                    if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
                        finish_reason = choice["finish_reason"]
                if stream_done:
                    break
        else:
            chat_resp = await call_backend_chat(cc, backend, upstream_model)
            if isinstance(chat_resp.get("usage"), dict):
                usage = chat_resp["usage"]
            choices = chat_resp.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] if isinstance(choices[0], dict) else {}
                msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                content = msg.get("content") or choice.get("text") or ""
                text_parts.append(str(content))
                if isinstance(choice.get("finish_reason"), str):
                    finish_reason = choice["finish_reason"]

        wall_ms = round((time.monotonic() - started) * 1000.0, 1)
        text = "".join(text_parts)
        completion_tokens, token_source = completion_token_count(usage, text)
        wall_seconds = max(wall_ms / 1000.0, 0.001)
        decode_tokens_per_sec: float | None = None
        if ttft_ms is not None and wall_ms > ttft_ms:
            decode_tokens_per_sec = round(completion_tokens / max((wall_ms - ttft_ms) / 1000.0, 0.001), 2)

        return {
            "schema": "nexus.model_benchmark.v1",
            "run_id": run_id,
            "created_at": created_at,
            "completed_at": now_unix(),
            "phase": phase,
            "run_index": run_index,
            "model": model,
            "ok": not stream_error,
            "error": stream_error,
            "backend": backend,
            "backend_class": backend_class,
            "resolved_model": upstream_model,
            "router_reason": route_reason,
            "stream": req.stream,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "completion_tokens": completion_tokens,
            "completion_tokens_source": token_source,
            "prompt_chars": len(req.prompt or ""),
            "response_chars": len(text),
            "wall_ms": wall_ms,
            "time_to_first_token_ms": ttft_ms,
            "tokens_per_sec": round(completion_tokens / wall_seconds, 2),
            "decode_tokens_per_sec": decode_tokens_per_sec,
            "chars_per_sec": round(len(text) / wall_seconds, 2),
            "finish_reason": finish_reason,
            "usage": usage,
        }
    except Exception as exc:
        return {
            "schema": "nexus.model_benchmark.v1",
            "run_id": run_id,
            "created_at": created_at,
            "completed_at": now_unix(),
            "phase": phase,
            "run_index": run_index,
            "model": model,
            "ok": False,
            "error": error_text(exc)[:4000],
            "backend": backend,
            "backend_class": backend_class,
            "resolved_model": upstream_model,
            "router_reason": route_reason,
            "stream": req.stream,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "wall_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    finally:
        if admission_acquired:
            admission.release(backend_class, "chat")


def append_result(result: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    target = path or benchmark_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(result, separators=(",", ":"), sort_keys=True, default=str))
            handle.write("\n")
    except Exception:
        # Benchmark persistence should not make the UI request fail.
        return


def summarize(results: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    by_model: OrderedDict[str, list[Dict[str, Any]]] = OrderedDict()
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
        first_ok = ok_results[0] if ok_results else {}
        rows.append(
            {
                "model": model,
                "runs": len(model_results),
                "ok": len(ok_results),
                "tokens_per_sec_avg": round(statistics.mean(tps_values), 2) if tps_values else None,
                "tokens_per_sec_min": round(min(tps_values), 2) if tps_values else None,
                "decode_tokens_per_sec_avg": round(statistics.mean(decode_values), 2) if decode_values else None,
                "time_to_first_token_ms_avg": round(statistics.mean(ttft_values), 1) if ttft_values else None,
                "completion_tokens_avg": round(statistics.mean(token_values), 1) if token_values else None,
                "backend": first_ok.get("backend") or first_ok.get("backend_class") or "",
                "resolved_model": first_ok.get("resolved_model") or "",
                "router_reason": first_ok.get("router_reason") or "",
                "error": "; ".join(errors[:2]),
            }
        )
    return rows


async def run_benchmark(req: ModelBenchmarkRequest) -> Dict[str, Any]:
    req = validate_request(req)
    if _BENCHMARK_LOCK.locked():
        raise BenchmarkBusy("another benchmark is already running")

    async with _BENCHMARK_LOCK:
        run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        results: list[Dict[str, Any]] = []
        for model in req.models:
            for run_index in range(1, req.warmup_runs + 1):
                result = await run_once(req, model=model, phase="warmup", run_index=run_index, run_id=run_id)
                append_result(result)
                results.append(result)
            for run_index in range(1, req.runs + 1):
                result = await run_once(req, model=model, phase="measure", run_index=run_index, run_id=run_id)
                append_result(result)
                results.append(result)

        return {
            "ok": True,
            "run_id": run_id,
            "generated_at": now_unix(),
            "settings": {
                "models": req.models,
                "runs": req.runs,
                "warmup_runs": req.warmup_runs,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
                "top_p": req.top_p,
                "stream": req.stream,
            },
            "summary": summarize(results),
            "results": results,
            "recent": recent_runs(limit=5),
        }


def _read_recent_result_lines(path: Path, *, max_lines: int = RECENT_HISTORY_LINES) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except Exception:
        return []

    out: list[Dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("schema") == "nexus.model_benchmark.v1":
            out.append(item)
    return out


def recent_runs(*, limit: int = 5, path: Optional[Path] = None) -> list[Dict[str, Any]]:
    cap = max(1, min(int(limit or 5), 20))
    items = _read_recent_result_lines(path or benchmark_log_path())
    by_run: OrderedDict[str, list[Dict[str, Any]]] = OrderedDict()
    for item in items:
        run_id = str(item.get("run_id") or "").strip()
        if not run_id:
            continue
        by_run.setdefault(run_id, []).append(item)

    runs: list[Dict[str, Any]] = []
    for run_id, results in by_run.items():
        completed = max([float(item.get("completed_at") or 0) for item in results] or [0.0])
        measure_results = [item for item in results if item.get("phase") == "measure"]
        models = clean_model_list([str(item.get("model") or "") for item in measure_results])
        first = results[0] if results else {}
        runs.append(
            {
                "run_id": run_id,
                "completed_at": completed,
                "models": models,
                "settings": {
                    "runs": len([item for item in measure_results if item.get("model") == (models[0] if models else "")]),
                    "warmup_runs": len([item for item in results if item.get("phase") == "warmup" and item.get("model") == (models[0] if models else "")]),
                    "max_tokens": first.get("max_tokens"),
                    "temperature": first.get("temperature"),
                    "top_p": first.get("top_p"),
                    "stream": first.get("stream"),
                },
                "summary": summarize(results),
            }
        )
    runs.sort(key=lambda item: float(item.get("completed_at") or 0), reverse=True)
    return runs[:cap]


def _compact_successful_result(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model": item.get("model") or "",
        "run_id": item.get("run_id") or "",
        "completed_at": item.get("completed_at") or item.get("created_at") or 0,
        "backend": item.get("backend") or item.get("backend_class") or "",
        "resolved_model": item.get("resolved_model") or "",
        "max_tokens": item.get("max_tokens"),
        "completion_tokens": item.get("completion_tokens"),
        "tokens_per_sec": item.get("tokens_per_sec"),
        "decode_tokens_per_sec": item.get("decode_tokens_per_sec"),
        "time_to_first_token_ms": item.get("time_to_first_token_ms"),
    }


def latest_successful_by_model(*, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    items = _read_recent_result_lines(path or benchmark_log_path())
    latest: Dict[str, Dict[str, Any]] = {}
    latest_time: Dict[str, float] = {}
    for item in items:
        if item.get("phase") != "measure" or item.get("ok") is not True:
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        completed = float(item.get("completed_at") or item.get("created_at") or 0)
        if model not in latest_time or completed >= latest_time[model]:
            latest_time[model] = completed
            latest[model] = _compact_successful_result(item)
    return latest
