from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import model_tool_qualification as qual
from app import openai_routes


class _FakeAdmission:
    async def acquire(self, _backend: str, _route_kind: str) -> None:
        return None

    def release(self, _backend: str, _route_kind: str) -> None:
        return None


class _FakeRegistry:
    def resolve_backend_class(self, backend: str) -> str:
        return backend

    def get_backend(self, _backend: str):
        return None


def _tool_response(*, city: str = "Paris") -> dict:
    return {
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": qual.TOOL_NAME, "arguments": json.dumps({"city": city})},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def _setup_runner(monkeypatch, *, backend: str = "local_mlx", native_tools: bool = True):
    monkeypatch.setattr(qual, "get_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(openai_routes, "get_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(qual, "get_admission_controller", lambda: _FakeAdmission())
    monkeypatch.setattr(qual, "check_backend_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(qual, "backend_supports_tool_calling", lambda _backend: native_tools)
    monkeypatch.setattr(openai_routes, "backend_supports_tool_calling", lambda _backend: native_tools)
    monkeypatch.setattr(openai_routes, "get_aliases", lambda: {})
    monkeypatch.setattr(
        qual,
        "decide_route",
        lambda **_kwargs: SimpleNamespace(backend=backend, model="upstream-model", reason="direct:model"),
    )

    async def fake_capability(_backend: str, _capability: str) -> None:
        return None

    monkeypatch.setattr(qual, "check_capability", fake_capability)


@pytest.mark.asyncio
async def test_run_case_passes_structured_openai_tool_call(monkeypatch):
    _setup_runner(monkeypatch, native_tools=True)
    captured = {}

    async def fake_call(req, backend: str, model_name: str):
        captured["req"] = req
        captured["backend"] = backend
        captured["model_name"] = model_name
        return _tool_response(city="Paris")

    monkeypatch.setattr(qual, "call_backend_chat", fake_call)

    req = qual.ModelToolQualificationRequest(models=["fast"], include_stream=False, include_roundtrip=False)
    case = qual.qualification_cases(req)[0]
    result = await qual.run_case(req, model="fast", case=case, run_id="run")

    assert result["ok"] is True
    assert result["tool_calls_count"] == 1
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["tools"][0]["function"]["name"] == qual.TOOL_NAME
    assert routed["tool_choice"] == "auto"
    assert captured["backend"] == "local_mlx"
    assert captured["model_name"] == "upstream-model"


@pytest.mark.asyncio
async def test_run_case_fails_raw_tool_like_text(monkeypatch):
    _setup_runner(monkeypatch, native_tools=True)

    async def fake_call(_req, _backend: str, _model_name: str):
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": 'get_weather{"city":"Paris"}'},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(qual, "call_backend_chat", fake_call)

    req = qual.ModelToolQualificationRequest(models=["fast"], include_stream=False, include_roundtrip=False)
    case = qual.qualification_cases(req)[0]
    result = await qual.run_case(req, model="fast", case=case, run_id="run")

    assert result["ok"] is False
    assert "raw tool-like text" in result["error"]
    assert "get_weather" in result["raw_tool_like_snippet"]


@pytest.mark.asyncio
async def test_auto_case_fails_when_gateway_strips_unsupported_native_tools(monkeypatch):
    _setup_runner(monkeypatch, backend="local_vllm_fast", native_tools=False)
    called = False

    async def fake_call(_req, _backend: str, _model_name: str):
        nonlocal called
        called = True
        return _tool_response(city="Paris")

    monkeypatch.setattr(qual, "call_backend_chat", fake_call)

    req = qual.ModelToolQualificationRequest(models=["fast"], include_stream=False, include_roundtrip=False)
    case = qual.qualification_cases(req)[0]
    result = await qual.run_case(req, model="fast", case=case, run_id="run")

    assert result["ok"] is False
    assert called is False
    assert "tool fields were stripped" in result["error"]


@pytest.mark.asyncio
async def test_required_case_can_pass_vllm_guided_tool_choice_without_native_auto(monkeypatch):
    _setup_runner(monkeypatch, backend="local_vllm_fast", native_tools=False)
    captured = {}

    async def fake_call(req, _backend: str, _model_name: str):
        captured["req"] = req
        return _tool_response(city="Berlin")

    monkeypatch.setattr(qual, "call_backend_chat", fake_call)

    req = qual.ModelToolQualificationRequest(models=["vllm_fast"], include_stream=False, include_roundtrip=False)
    case = next(item for item in qual.qualification_cases(req) if item.name == "required_nonstream")
    result = await qual.run_case(req, model="vllm_fast", case=case, run_id="run")

    assert result["ok"] is True
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["tools"][0]["function"]["name"] == qual.TOOL_NAME
    assert routed["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_stream_response_reconstructs_tool_call_arguments():
    async def gen():
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\""}}]}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"Paris\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        yield b"data: [DONE]\n\n"

    response = await qual._collect_stream_response(gen())
    case = qual.ToolQualificationCase(
        name="stream",
        category="stream",
        prompt="",
        tool_choice="auto",
        expect_tool=True,
        expected_city="Paris",
        stream=True,
    )

    result = qual.evaluate_tool_response(response, case)

    assert result["ok"] is True
    assert result["tool_calls"][0]["arguments"] == {"city": "Paris"}


def test_latest_by_model_keeps_latest_failure_for_ui(tmp_path):
    path = tmp_path / "tools.jsonl"
    older = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "older",
        "completed_at": 10,
        "model": "fast",
        "ok": True,
        "summary": {"passed": 5, "total": 5, "first_error": "", "by_category": {"auto": {"passed": 2, "total": 2}}},
    }
    newer = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "newer",
        "completed_at": 20,
        "model": "fast",
        "ok": False,
        "backend": "local_vllm",
        "resolved_model": "upstream-model",
        "summary": {"passed": 3, "total": 5, "first_error": "auto failed", "by_category": {"auto": {"passed": 0, "total": 2}}},
    }
    path.write_text("\n".join(json.dumps(item) for item in [older, newer]) + "\n", encoding="utf-8")

    latest = qual.latest_by_model(path=path)

    assert latest["fast"]["run_id"] == "newer"
    assert latest["fast"]["ok"] is False
    assert latest["fast"]["first_error"] == "auto failed"
    assert latest["local_vllm:upstream-model"]["run_id"] == "newer"


def test_latest_by_model_indexes_resolved_backend_class(tmp_path):
    path = tmp_path / "tools.jsonl"
    item = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "resolved",
        "completed_at": 20,
        "model": "fast",
        "backend": "route-vllm-fast",
        "backend_class": "local_vllm_fast",
        "resolved_model": "upstream-model",
        "ok": True,
        "summary": {"passed": 1, "total": 1, "first_error": "", "by_category": {"auto": {"passed": 1, "total": 1}}},
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    latest = qual.latest_by_model(path=path)

    assert latest["route-vllm-fast:upstream-model"]["run_id"] == "resolved"
    assert latest["local_vllm_fast:upstream-model"]["run_id"] == "resolved"
    assert latest["local_vllm_fast:upstream-model"]["backend_class"] == "local_vllm_fast"


def test_qualification_cases_include_client_transcript_shapes():
    req = qual.ModelToolQualificationRequest(models=["fast"], include_stream=False, include_roundtrip=False)
    cases = qual.qualification_cases(req)

    assert any(case.name == "continue_tool_history_nonstream" and case.client_shape == "continue_tool_history" for case in cases)
    assert any(case.name == "hermes_tool_history_nonstream" and case.client_shape == "hermes_tool_history" for case in cases)

    for case in cases:
        if case.client_shape not in {"continue_tool_history", "hermes_tool_history"}:
            continue
        messages = qual._client_shape_messages(case)
        assert messages is not None
        assert [message.role for message in messages][-2:] == ["assistant", "tool"]
        assert qual.TOOL_RESULT_MARKER in str(messages[1].content)


def test_evaluate_tool_response_rejects_bare_raw_tool_text():
    case = qual.ToolQualificationCase(
        name="auto",
        category="auto",
        prompt="",
        tool_choice="auto",
        expect_tool=True,
        expected_city="Paris",
    )

    result = qual.evaluate_tool_response(
        {"choices": [{"message": {"role": "assistant", "content": 'get_weather{"city":"Paris"}'}, "finish_reason": "stop"}]},
        case,
    )

    assert result["ok"] is False
    assert "raw tool-like text" in result["error"]
    assert result["raw_tool_like_snippet"]


def test_evaluate_tool_response_rejects_contaminated_tool_call_name():
    case = qual.ToolQualificationCase(
        name="auto",
        category="auto",
        prompt="",
        tool_choice="auto",
        expect_tool=True,
        expected_city="Paris",
    )
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "grep_dirs</arg_value>pattern</arg_key><arg_value>stackrot</arg_value>",
                                "arguments": json.dumps({"city": "Paris"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    result = qual.evaluate_tool_response(response, case)

    assert result["ok"] is False
    assert "malformed tool name" in result["error"]
    assert result["tool_calls"][0]["name"].startswith("grep_dirs</arg_value>")


def test_qualification_status_blocks_failed_result(tmp_path):
    path = tmp_path / "tools.jsonl"
    item = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "failed",
        "completed_at": 100,
        "model": "fast",
        "backend": "local_vllm",
        "resolved_model": "upstream-model",
        "ok": False,
        "summary": {"passed": 1, "total": 2, "first_error": "auto failed", "by_category": {"auto": {"passed": 0, "total": 1}}},
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    status = qual.qualification_status_for_target(
        alias_name="fast",
        backend_class="local_vllm",
        resolved_model="upstream-model",
        tool_choice="auto",
        path=path,
        now=120,
    )

    assert status["qualified"] is False
    assert status["failed"] is True
    assert "auto failed" in status["reason"]


def test_qualification_status_allows_passed_category_when_other_category_failed(tmp_path):
    path = tmp_path / "tools.jsonl"
    item = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "client-history-failed",
        "completed_at": 100,
        "model": "fast",
        "backend": "local_vllm",
        "resolved_model": "upstream-model",
        "ok": False,
        "summary": {
            "passed": 8,
            "total": 9,
            "first_error": "client history failed",
            "by_category": {
                "auto": {"passed": 2, "total": 2},
                "client_continue": {"passed": 0, "total": 1},
            },
        },
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    status = qual.qualification_status_for_target(
        alias_name="fast",
        backend_class="local_vllm",
        resolved_model="upstream-model",
        tool_choice="auto",
        path=path,
        now=120,
    )

    assert status["qualified"] is True


def test_qualification_status_blocks_stale_result(tmp_path, monkeypatch):
    monkeypatch.setattr(qual.S, "MODEL_TOOL_QUALIFICATION_MAX_AGE_SEC", 10, raising=False)
    path = tmp_path / "tools.jsonl"
    item = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "old",
        "completed_at": 100,
        "model": "fast",
        "backend": "local_vllm",
        "resolved_model": "upstream-model",
        "ok": True,
        "summary": {"passed": 2, "total": 2, "first_error": "", "by_category": {"auto": {"passed": 1, "total": 1}}},
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    status = qual.qualification_status_for_target(
        alias_name="fast",
        backend_class="local_vllm",
        resolved_model="upstream-model",
        tool_choice="auto",
        path=path,
        now=120,
    )

    assert status["qualified"] is False
    assert status["stale"] is True


def test_guardrail_allows_stale_success_when_results_are_optional(monkeypatch):
    monkeypatch.setattr(
        qual.S,
        "MODEL_TOOL_QUALIFICATION_GUARDRAIL_REQUIRE_RESULT",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        qual,
        "qualification_status_for_target",
        lambda **_kwargs: {
            "qualified": False,
            "stale": True,
            "category": "auto",
            "result": {
                "ok": True,
                "by_category": {"auto": {"passed": 1, "total": 1}},
            },
        },
    )

    assert (
        qual.guardrail_reason_for_target(
            alias_name="long",
            backend_class="local_mlx",
            resolved_model="mlx-model",
            tool_choice="auto",
        )
        is None
    )


def test_guardrail_blocks_stale_success_when_results_are_required(monkeypatch):
    monkeypatch.setattr(
        qual.S,
        "MODEL_TOOL_QUALIFICATION_GUARDRAIL_REQUIRE_RESULT",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        qual,
        "qualification_status_for_target",
        lambda **_kwargs: {
            "qualified": False,
            "stale": True,
            "result": {"ok": True},
            "reason": "tool qualification is stale",
        },
    )

    assert (
        qual.guardrail_reason_for_target(
            alias_name="long",
            backend_class="local_mlx",
            resolved_model="mlx-model",
            tool_choice="auto",
        )
        == "tool qualification is stale"
    )


def test_guardrail_blocks_stale_aggregate_success_when_requested_category_failed(monkeypatch):
    monkeypatch.setattr(
        qual.S,
        "MODEL_TOOL_QUALIFICATION_GUARDRAIL_REQUIRE_RESULT",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        qual,
        "qualification_status_for_target",
        lambda **_kwargs: {
            "qualified": False,
            "stale": True,
            "category": "auto",
            "result": {
                "ok": True,
                "by_category": {"auto": {"passed": 0, "total": 1}},
            },
            "reason": "tool qualification is stale",
        },
    )

    assert (
        qual.guardrail_reason_for_target(
            alias_name="fast",
            backend_class="local_vllm_fast",
            resolved_model="fast-model",
            tool_choice="auto",
        )
        == "tool qualification is stale"
    )


def test_qualification_status_blocks_alias_target_mismatch(tmp_path):
    path = tmp_path / "tools.jsonl"
    item = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "old-target",
        "completed_at": 100,
        "model": "fast",
        "backend": "local_vllm",
        "resolved_model": "old-model",
        "ok": True,
        "summary": {"passed": 2, "total": 2, "first_error": "", "by_category": {"auto": {"passed": 1, "total": 1}}},
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    status = qual.qualification_status_for_target(
        alias_name="fast",
        backend_class="local_vllm",
        resolved_model="new-model",
        tool_choice="auto",
        path=path,
        now=120,
    )

    assert status["qualified"] is False
    assert status["mismatch"] is True


def test_qualification_status_accepts_resolved_backend_class_match(tmp_path):
    path = tmp_path / "tools.jsonl"
    item = {
        "schema": qual.SCHEMA_VERSION,
        "run_id": "resolved-target",
        "completed_at": 100,
        "model": "fast",
        "backend": "route-vllm-fast",
        "backend_class": "local_vllm_fast",
        "resolved_model": "upstream-model",
        "ok": True,
        "summary": {"passed": 1, "total": 1, "first_error": "", "by_category": {"auto": {"passed": 1, "total": 1}}},
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    status = qual.qualification_status_for_target(
        alias_name="fast",
        backend_class="local_vllm_fast",
        resolved_model="upstream-model",
        tool_choice="auto",
        path=path,
        now=120,
    )

    assert status["qualified"] is True


def test_qualification_status_allows_missing_result_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(qual.S, "MODEL_TOOL_QUALIFICATION_GUARDRAIL_REQUIRE_RESULT", False, raising=False)

    status = qual.qualification_status_for_target(
        alias_name="fast",
        backend_class="local_vllm",
        resolved_model="new-model",
        tool_choice="auto",
        path=tmp_path / "missing.jsonl",
        now=120,
    )

    assert status["qualified"] is True
    assert status["missing"] is True


@pytest.mark.asyncio
async def test_auto_qualification_candidates_include_missing_target(monkeypatch):
    _setup_runner(monkeypatch, backend="local_vllm", native_tools=True)
    monkeypatch.setattr(qual, "latest_by_model", lambda **_kwargs: {})

    candidates = await qual.auto_qualification_candidates(["fast"])

    assert candidates == ["fast"]


@pytest.mark.asyncio
async def test_auto_qualification_candidates_include_incomplete_suite(monkeypatch):
    _setup_runner(monkeypatch, backend="local_vllm", native_tools=True)
    monkeypatch.setattr(
        qual,
        "latest_by_model",
        lambda **_kwargs: {
            "local_vllm:upstream-model": {
                "ok": True,
                "completed_at": 100,
                "backend": "local_vllm",
                "resolved_model": "upstream-model",
                "by_category": {"auto": {"passed": 1, "total": 1}},
            }
        },
    )

    candidates = await qual.auto_qualification_candidates(["fast"])

    assert candidates == ["fast"]


@pytest.mark.asyncio
async def test_auto_qualification_candidates_include_failed_non_auto_case(monkeypatch):
    _setup_runner(monkeypatch, backend="local_vllm", native_tools=True)
    complete_categories = {
        category: {"passed": 1, "total": 1}
        for category in qual._expected_categories(include_stream=True, include_roundtrip=True)
    }
    complete_categories["auto"] = {"passed": 2, "total": 2}
    complete_categories["stream"] = {"passed": 0, "total": 1}
    monkeypatch.setattr(
        qual,
        "latest_by_model",
        lambda **_kwargs: {
            "local_vllm:upstream-model": {
                "ok": False,
                "completed_at": qual.now_unix(),
                "backend": "local_vllm",
                "resolved_model": "upstream-model",
                "by_category": complete_categories,
            }
        },
    )

    candidates = await qual.auto_qualification_candidates(["fast"])

    assert candidates == ["fast"]
