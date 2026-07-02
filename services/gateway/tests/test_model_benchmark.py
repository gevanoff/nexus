from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app import model_benchmark as admin_benchmark


def _load_benchmark_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "tools" / "model_benchmark.py"
    spec = importlib.util.spec_from_file_location("nexus_model_benchmark", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_base_v1_accepts_root_v1_and_chat_urls():
    bench = _load_benchmark_module()

    assert bench._base_v1("http://127.0.0.1:8800") == "http://127.0.0.1:8800/v1"
    assert bench._base_v1("http://127.0.0.1:8800/v1") == "http://127.0.0.1:8800/v1"
    assert bench._base_v1("http://127.0.0.1:8800/v1/chat/completions") == "http://127.0.0.1:8800/v1"


def test_parse_sse_payload_handles_done_json_and_noise():
    bench = _load_benchmark_module()

    assert bench._parse_sse_payload(b": keepalive\n") is None
    assert bench._parse_sse_payload(b"data: [DONE]\n") == "[DONE]"
    assert bench._parse_sse_payload(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n') == {
        "choices": [{"delta": {"content": "hi"}}]
    }


def test_completion_token_count_prefers_usage_then_estimates():
    bench = _load_benchmark_module()

    assert bench._completion_token_count({"completion_tokens": 42}, "one two")[0:2] == (42, "usage")
    tokens, source = bench._completion_token_count({}, "one two three four")

    assert source == "estimated"
    assert tokens >= 5


def test_expand_models_deduplicates_repeated_and_comma_values():
    bench = _load_benchmark_module()

    assert bench._expand_models(["fast,coder", "fast", " default "]) == ["fast", "coder", "default"]


def test_summary_uses_only_measurement_runs():
    bench = _load_benchmark_module()

    rows = bench._summarize(
        [
            {"phase": "warmup", "model": "fast", "ok": True, "tokens_per_sec": 1},
            {
                "phase": "measure",
                "model": "fast",
                "ok": True,
                "tokens_per_sec": 10.0,
                "decode_tokens_per_sec": 20.0,
                "time_to_first_token_ms": 100.0,
                "completion_tokens": 5,
                "headers": {"x-backend-used": "local_vllm", "x-model-used": "actual"},
            },
            {
                "phase": "measure",
                "model": "fast",
                "ok": True,
                "tokens_per_sec": 20.0,
                "decode_tokens_per_sec": 40.0,
                "time_to_first_token_ms": 200.0,
                "completion_tokens": 7,
                "headers": {},
            },
        ]
    )

    assert rows == [
        {
            "model": "fast",
            "runs": 2,
            "ok": 2,
            "tokens_per_sec_avg": 15.0,
            "tokens_per_sec_min": 10.0,
            "decode_tokens_per_sec_avg": 30.0,
            "ttft_ms_avg": 150.0,
            "completion_tokens_avg": 6.0,
            "backend": "local_vllm",
            "resolved_model": "actual",
            "error": "",
        }
    ]


def test_admin_benchmark_validation_cleans_models_and_enforces_limits():
    req = admin_benchmark.ModelBenchmarkRequest(models=["fast,coder", "fast"], runs=2, warmup_runs=0, max_tokens=64)

    clean = admin_benchmark.validate_request(req)

    assert clean.models == ["fast", "coder"]


def test_admin_benchmark_iter_sse_payloads_handles_multi_event_chunk():
    events = list(
        admin_benchmark.iter_sse_payloads(
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"usage":{"completion_tokens":12}}\n\n'
            b"data: [DONE]\n\n"
        )
    )

    assert events == [
        {"choices": [{"delta": {"content": "a"}}]},
        {"usage": {"completion_tokens": 12}},
        "[DONE]",
    ]


def test_admin_benchmark_recent_runs_groups_jsonl_by_run_id(tmp_path):
    path = tmp_path / "bench.jsonl"
    items = [
        {
            "schema": "nexus.model_benchmark.v1",
            "run_id": "older",
            "completed_at": 10,
            "phase": "measure",
            "model": "fast",
            "ok": True,
            "tokens_per_sec": 5.0,
            "completion_tokens": 5,
            "max_tokens": 32,
        },
        {
            "schema": "nexus.model_benchmark.v1",
            "run_id": "newer",
            "completed_at": 20,
            "phase": "measure",
            "model": "coder",
            "ok": True,
            "tokens_per_sec": 10.0,
            "decode_tokens_per_sec": 12.0,
            "time_to_first_token_ms": 100.0,
            "completion_tokens": 8,
            "backend": "local_mlx",
            "resolved_model": "actual",
            "max_tokens": 64,
        },
    ]
    path.write_text("\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8")

    recent = admin_benchmark.recent_runs(limit=2, path=path)

    assert [item["run_id"] for item in recent] == ["newer", "older"]
    assert recent[0]["summary"][0]["tokens_per_sec_avg"] == 10.0
    assert recent[0]["summary"][0]["backend"] == "local_mlx"
