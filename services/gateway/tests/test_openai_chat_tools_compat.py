from __future__ import annotations

import logging
import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import model_tool_qualification, openai_routes
from app.models import ChatCompletionRequest


class _FakeAdmission:
    async def acquire(self, _backend: str, _route_kind: str) -> None:
        return None

    def release(self, _backend: str, _route_kind: str) -> None:
        return None


class _FakeRegistry:
    def resolve_backend_class(self, backend_name: str) -> str:
        return backend_name

    def get_backend(self, _backend_name: str):
        return None


def _build_client(
    monkeypatch,
    *,
    backend_name: str = "local_vllm",
    backend_supports_tools: bool = False,
    chat_handler=None,
    stream_handler=None,
    patch_auth: bool = True,
) -> TestClient:
    if patch_auth:
        monkeypatch.setattr(openai_routes, "require_bearer", lambda _req: None)
    monkeypatch.setattr(openai_routes, "check_backend_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(openai_routes, "get_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(openai_routes, "get_admission_controller", lambda: _FakeAdmission())
    monkeypatch.setattr(openai_routes, "get_aliases", lambda: {})
    monkeypatch.setattr(openai_routes, "llm_backends", lambda: [])
    monkeypatch.setattr(openai_routes, "backend_supports_tool_calling", lambda _backend: backend_supports_tools)
    monkeypatch.setattr(
        openai_routes,
        "decide_route",
        lambda **_kwargs: SimpleNamespace(backend=backend_name, model="upstream-model", reason="test-route"),
    )

    async def _inject_memory(messages, req):
        return messages

    async def _check_capability(_backend: str, _capability: str) -> None:
        return None

    async def _default_call_backend_chat(req, backend: str, model_name: str):
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "model": f"{backend}:{model_name}",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    def _default_stream_backend_chat(req, backend: str, model_name: str):
        async def gen():
            yield b'data: {"id":"chatcmpl_test","object":"chat.completion.chunk"}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(openai_routes, "inject_memory", _inject_memory)
    monkeypatch.setattr(openai_routes, "check_capability", _check_capability)
    monkeypatch.setattr(openai_routes, "call_backend_chat", chat_handler or _default_call_backend_chat)

    def _stream_backend_chat_as_openai(req, backend: str, model_name: str, **_kwargs):
        return (stream_handler or _default_stream_backend_chat)(req, backend, model_name)

    monkeypatch.setattr(openai_routes, "stream_backend_chat_as_openai", _stream_backend_chat_as_openai)

    app = FastAPI()
    app.include_router(openai_routes.router)
    return TestClient(app)


def test_chat_completion_request_allows_openai_tool_fields():
    req = ChatCompletionRequest(
        model="continue-local",
        messages=[
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "demo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "done", "extra_tool_field": True},
        ],
        tools=[{"type": "function", "function": {"name": "demo", "parameters": {}, "strict": True}, "vendor_hint": "preserve"}],
        tool_choice="auto",
        parallel_tool_calls=True,
        response_format={"type": "json_schema"},
        metadata={"client": "continue"},
        store=False,
        stream_options={"include_usage": True},
        logprobs=False,
        top_logprobs=0,
        n=1,
        unknown_future_field="keep-me",
    )

    payload = req.model_dump(exclude_none=True)

    assert payload["parallel_tool_calls"] is True
    assert payload["unknown_future_field"] == "keep-me"
    assert payload["tools"][0]["vendor_hint"] == "preserve"
    assert payload["messages"][1]["extra_tool_field"] is True


def test_chat_completions_drops_stream_options_for_local_mlx_stream(monkeypatch):
    captured_requests = []

    def _stream_handler(req, _backend: str, _model_name: str):
        captured_requests.append(req)

        async def gen():
            yield b'data: {"id":"chatcmpl_test","object":"chat.completion.chunk"}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(
        monkeypatch,
        backend_name="local_mlx",
        backend_supports_tools=True,
        stream_handler=_stream_handler,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "long",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    assert response.status_code == 200
    assert captured_requests
    assert captured_requests[0].stream_options is None


def test_chat_completions_preserves_stream_options_for_other_backends(monkeypatch):
    captured_requests = []

    def _stream_handler(req, _backend: str, _model_name: str):
        captured_requests.append(req)

        async def gen():
            yield b'data: {"id":"chatcmpl_test","object":"chat.completion.chunk"}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(
        monkeypatch,
        backend_name="local_vllm",
        backend_supports_tools=True,
        stream_handler=_stream_handler,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "long",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    assert response.status_code == 200
    assert captured_requests
    assert captured_requests[0].stream_options == {"include_usage": True}


def test_selected_alias_name_prefers_router_fallback_alias(monkeypatch):
    monkeypatch.setattr(openai_routes, "get_aliases", lambda: {"fast": object(), "long": object()})

    assert openai_routes._selected_alias_name("fast", "policy:alias_context->fast->alias:long") == "long"


def test_models_route_still_requires_and_accepts_bearer_auth(monkeypatch):
    client = _build_client(monkeypatch, patch_auth=False)

    denied = client.get("/v1/models")
    allowed = client.get("/v1/models", headers={"Authorization": "Bearer test-token"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["object"] == "list"
    assert any(item["id"] == "auto" for item in body["data"])


def test_chat_completions_returns_openai_error_for_invalid_request_shape(monkeypatch):
    client = _build_client(monkeypatch)

    response = client.post("/v1/chat/completions", json={"model": "continue-local", "messages": "not-a-list"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["param"] == "messages"
    assert "invalid request shape" in body["error"]["message"]


def test_chat_completions_non_stream_without_tools_still_works(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "NEXUS_DIRECT_OK"}, "finish_reason": "stop"}],
        }

    client = _build_client(monkeypatch, chat_handler=_chat_handler)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "Reply with exactly NEXUS_DIRECT_OK"}],
            "temperature": 0,
            "max_tokens": 20,
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "NEXUS_DIRECT_OK"
    assert "tools" not in captured["req"].model_dump(exclude_none=True)


def test_chat_completions_stream_without_tools_still_works(monkeypatch):
    captured = {}

    def _stream_handler(req, backend: str, model_name: str):
        captured["req"] = req

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"NEXUS_STREAM_OK"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(monkeypatch, stream_handler=_stream_handler)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "Reply with exactly NEXUS_STREAM_OK"}],
            "temperature": 0,
            "max_tokens": 20,
            "stream": True,
        },
    ) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert b"NEXUS_STREAM_OK" in body
    assert "tools" not in captured["req"].model_dump(exclude_none=True)


def test_completions_non_stream_shims_to_chat_and_returns_legacy_shape(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        captured["backend"] = backend
        captured["model_name"] = model_name
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "def add(a, b): return a + b"}, "finish_reason": "stop"}],
        }

    client = _build_client(monkeypatch, chat_handler=_chat_handler)

    response = client.post(
        "/v1/completions",
        json={
            "model": "fast",
            "prompt": "Write add.",
            "max_tokens": 64,
            "temperature": 0,
            "top_p": 0.5,
            "stop": ["\n\n"],
            "stream": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "def add(a, b): return a + b"
    assert body["usage"]["total_tokens"] == 5
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["messages"] == [{"role": "user", "content": "Write add."}]
    assert routed["top_p"] == 0.5
    assert routed["stop"] == ["\n\n"]


def test_completions_stream_returns_legacy_chunks_and_done(monkeypatch):
    def _stream_handler(_req, _backend: str, _model_name: str):
        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"def add"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"(a, b):"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(monkeypatch, stream_handler=_stream_handler)

    with client.stream(
        "POST",
        "/v1/completions",
        json={
            "model": "fast",
            "prompt": "Write add.",
            "max_tokens": 64,
            "temperature": 0,
            "stream": True,
        },
    ) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert b'"object":"text_completion.chunk"' in body
    assert b'"text":"def add"' in body
    assert b'"text":"(a, b):"' in body
    assert b"data: [DONE]\n\n" in body


def test_completions_rejects_invalid_prompt_with_request_id(monkeypatch):
    client = _build_client(monkeypatch)

    response = client.post(
        "/v1/completions",
        json={"model": "fast", "prompt": [{"type": "text", "text": "not legacy"}], "stream": False},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "prompt"
    assert "prompt must be a string or list of strings" in body["error"]["message"]
    assert "request_id=" in body["error"]["message"]


def test_completions_stream_fills_empty_upstream_error(monkeypatch):
    def _stream_handler(_req, _backend: str, _model_name: str):
        async def gen():
            yield b'data: {"error":{"message":"","type":"server_error","param":null,"code":"500"}}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(monkeypatch, stream_handler=_stream_handler)

    with client.stream(
        "POST",
        "/v1/completions",
        json={"model": "fast", "prompt": "Write add.", "stream": True},
    ) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert b'"message":"Gateway request failed;' in body
    assert b"request_id=" in body
    assert b"data: [DONE]\n\n" in body


def test_chat_completions_degrades_streaming_tools_for_unsupported_backend(monkeypatch):
    captured = {}

    def _stream_handler(req, backend: str, model_name: str):
        captured["req"] = req
        captured["backend"] = backend
        captured["model_name"] = model_name

        async def gen():
            yield b'data: {"id":"chatcmpl_test","object":"chat.completion.chunk"}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(monkeypatch, backend_supports_tools=False, stream_handler=_stream_handler)

    payload = {
        "model": "continue-local",
        "stream": True,
        "messages": [
            {"role": "user", "content": "use a tool if needed"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "demo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
        ],
        "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert b"[DONE]" in body
    routed = captured["req"].model_dump(exclude_none=True)
    assert "tools" not in routed
    assert "tool_choice" not in routed
    assert "parallel_tool_calls" not in routed
    assert routed["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert routed["messages"][2]["role"] == "tool"


def test_chat_completions_forwards_native_tools_without_server_tool_loop(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        captured["backend"] = backend
        captured["model_name"] = model_name
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "demo", "arguments": "{}"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

    client = _build_client(monkeypatch, backend_supports_tools=True, chat_handler=_chat_handler)

    payload = {
        "model": "continue-local",
        "messages": [{"role": "user", "content": "use a tool if needed"}],
        "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["tools"][0]["function"]["name"] == "demo"
    assert routed["tool_choice"] == "auto"
    assert routed["parallel_tool_calls"] is True
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"


def test_chat_completions_degrades_auto_tools_when_latest_qualification_failed(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "QUAL_GUARD_OK"}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr(
        model_tool_qualification,
        "latest_by_model",
        lambda **_kwargs: {
            "local_vllm:upstream-model": {
                "ok": False,
                "completed_at": 100,
                "backend": "local_vllm",
                "resolved_model": "upstream-model",
                "first_error": "auto failed",
                "by_category": {"auto": {"passed": 0, "total": 1}},
            }
        },
    )
    monkeypatch.setattr(model_tool_qualification, "now_unix", lambda: 120)
    client = _build_client(monkeypatch, backend_supports_tools=True, chat_handler=_chat_handler)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "continue-local",
            "messages": [{"role": "user", "content": "use a tool if needed"}],
            "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )

    assert response.status_code == 200
    routed = captured["req"].model_dump(exclude_none=True)
    assert "tools" not in routed
    assert "tool_choice" not in routed


def test_chat_completions_rejects_required_tools_when_latest_qualification_failed(monkeypatch):
    monkeypatch.setattr(
        model_tool_qualification,
        "latest_by_model",
        lambda **_kwargs: {
            "local_vllm:upstream-model": {
                "ok": False,
                "completed_at": 100,
                "backend": "local_vllm",
                "resolved_model": "upstream-model",
                "first_error": "required failed",
                "by_category": {"required": {"passed": 0, "total": 1}},
            }
        },
    )
    monkeypatch.setattr(model_tool_qualification, "now_unix", lambda: 120)
    client = _build_client(monkeypatch, backend_supports_tools=True)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "continue-local",
            "messages": [{"role": "user", "content": "must call a tool"}],
            "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
            "tool_choice": "required",
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["param"] == "tool_choice"
    assert "tool qualification guardrail" in body["error"]["message"]


def test_chat_completions_logs_tool_handling_without_message_content_by_default(monkeypatch, caplog):
    client = _build_client(monkeypatch, backend_supports_tools=False)

    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": "secret prompt text"}],
                "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
                "tool_choice": "auto",
                "parallel_tool_calls": True,
            },
        )

    assert response.status_code == 200
    debug_messages = [record.getMessage() for record in caplog.records]
    assert any("request_keys" in message for message in debug_messages)
    assert any("tool_fields=stripped" in message for message in debug_messages)
    assert all("secret prompt text" not in message for message in debug_messages)


def test_chat_completions_alias_tools_false_degrades_instead_of_400(monkeypatch):
    class _Alias(BaseModel):
        backend: str
        upstream_model: str
        tools: bool | None = None
        temperature_cap: float | None = None
        max_tokens_cap: int | None = None

    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "TOOL_COMPAT_OK"}, "finish_reason": "stop"}],
        }

    client = _build_client(monkeypatch, backend_supports_tools=True, chat_handler=_chat_handler)
    monkeypatch.setattr(
        openai_routes,
        "get_aliases",
        lambda: {"fast": _Alias(backend="local_vllm_fast", upstream_model="upstream-model", tools=False)},
    )
    monkeypatch.setattr(
        openai_routes,
        "decide_route",
        lambda **_kwargs: SimpleNamespace(backend="local_vllm_fast", model="upstream-model", reason="alias:model"),
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "Reply with exactly TOOL_COMPAT_OK"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "temperature": 0,
            "max_tokens": 50,
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "TOOL_COMPAT_OK"
    routed = captured["req"].model_dump(exclude_none=True)
    assert "tools" not in routed
    assert "tool_choice" not in routed
    assert "parallel_tool_calls" not in routed


def test_chat_completions_returns_openai_error_when_tools_are_required(monkeypatch):
    client = _build_client(monkeypatch, backend_name="legacy_chat", backend_supports_tools=False)

    payload = {
        "model": "continue-local",
        "messages": [{"role": "user", "content": "must call a tool"}],
        "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
        "tool_choice": "required",
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "tool_choice"
    assert "explicitly required" in body["error"]["message"]


def test_chat_completions_forwards_required_tools_to_vllm_guided_decoding(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        captured["backend"] = backend
        captured["model_name"] = model_name
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "demo", "arguments": "{}"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

    client = _build_client(
        monkeypatch,
        backend_name="local_vllm_fast",
        backend_supports_tools=False,
        chat_handler=_chat_handler,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "vllm_fast",
            "messages": [{"role": "user", "content": "must call a tool"}],
            "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
            "tool_choice": "required",
        },
    )

    assert response.status_code == 200
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["tools"][0]["function"]["name"] == "demo"
    assert routed["tool_choice"] == "required"
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"


def test_chat_completions_forwards_named_tools_to_vllm_guided_decoding(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "demo", "arguments": "{}"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

    client = _build_client(
        monkeypatch,
        backend_name="local_vllm",
        backend_supports_tools=False,
        chat_handler=_chat_handler,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "vllm",
            "messages": [{"role": "user", "content": "must call demo"}],
            "tools": [{"type": "function", "function": {"name": "demo", "parameters": {}}}],
            "tool_choice": {"type": "function", "function": {"name": "demo"}},
        },
    )

    assert response.status_code == 200
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["tools"][0]["function"]["name"] == "demo"
    assert routed["tool_choice"] == {"type": "function", "function": {"name": "demo"}}


def test_responses_shims_tool_fields_and_preserves_tool_calls(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }

    client = _build_client(monkeypatch, backend_supports_tools=True, chat_handler=_chat_handler)

    response = client.post(
        "/v1/responses",
        json={
            "model": "fast",
            "input": [{"role": "user", "content": "use a tool"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        },
    )

    assert response.status_code == 200
    routed = captured["req"].model_dump(exclude_none=True)
    assert routed["parallel_tool_calls"] is True
    output = response.json()["output"]
    assert any(item["type"] == "function_call" and item["call_id"] == "call_1" for item in output)


def test_responses_stream_degrades_tools_for_unsupported_backend(monkeypatch):
    captured = {}

    def _stream_handler(req, backend: str, model_name: str):
        captured["req"] = req

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return gen()

    client = _build_client(monkeypatch, backend_supports_tools=False, stream_handler=_stream_handler)

    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "fast",
            "input": [{"role": "user", "content": "use a tool"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "stream": True,
        },
    ) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert b"response.completed" in body
    routed = captured["req"].model_dump(exclude_none=True)
    assert "tools" not in routed
    assert "tool_choice" not in routed
    assert "parallel_tool_calls" not in routed



def test_chat_completions_normalizes_continue_style_request_shape(monkeypatch):
    captured = {}

    async def _chat_handler(req, backend: str, model_name: str):
        captured["req"] = req
        return {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    client = _build_client(monkeypatch, backend_supports_tools=True, chat_handler=_chat_handler)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "continue-local",
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "thinking", "content": "internal scratchpad"},
                {"role": "user", "content": [{"type": "text", "text": "read README.md"}]},
                {
                    "role": "assistant",
                    "content": "",
                    "toolCalls": [
                        {
                            "id": "call_1",
                            "function": {"name": "read_file", "arguments": {"filepath": "README.md"}},
                        }
                    ],
                },
                {"role": "tool", "toolCallId": "call_1", "content": "README content"},
            ],
            "completionOptions": {
                "tools": [
                    {
                        "type": "function",
                        "displayTitle": "Read File",
                        "wouldLikeTo": "read {{{ filepath }}}",
                        "readonly": True,
                        "group": "Built-In",
                        "function": {"name": "read_file", "parameters": {}},
                        "systemMessageDescription": {"prefix": "Use read_file."},
                        "defaultToolPolicy": "allowedWithoutPermission",
                        "toolCallIcon": "DocumentIcon",
                    }
                ],
                "toolChoice": "auto",
                "maxTokens": 128,
                "temperature": 0.2,
                "topP": 0.5,
                "reasoning": False,
            },
            "reasoning": False,
        },
    )

    assert response.status_code == 200
    routed = captured["req"].model_dump(exclude_none=True)
    assert "completionOptions" not in routed
    assert "reasoning" not in routed
    assert routed["tool_choice"] == "auto"
    assert routed["max_tokens"] == 128
    assert routed["temperature"] == 0.2
    assert routed["top_p"] == 0.5
    assert routed["tools"][0]["function"]["name"] == "read_file"
    assert sorted(routed["tools"][0].keys()) == ["function", "type"]
    assert "displayTitle" not in routed["tools"][0]
    assert [message["role"] for message in routed["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert captured["req"].messages[2].content is None
    assert routed["messages"][1]["content"] == "read README.md"
    assert "tool_calls" in routed["messages"][2]
    assert "toolCalls" not in routed["messages"][2]
    assert routed["messages"][2]["tool_calls"][0]["type"] == "function"
    assert routed["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"filepath":"README.md"}'
    assert routed["messages"][3]["tool_call_id"] == "call_1"
    assert "toolCallId" not in routed["messages"][3]


def test_chat_completions_rejects_unsupported_continue_content_array(monkeypatch):
    client = _build_client(monkeypatch, backend_supports_tools=True)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "continue-local",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.invalid/image.png"},
                        }
                    ],
                }
            ],
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "messages.0.content"
    assert "unsupported array content" in body["error"]["message"]
    assert "request_id=" in body["error"]["message"]


def test_chat_completions_debug_diagnostics_are_redacted(monkeypatch, caplog):
    client = _build_client(monkeypatch, backend_supports_tools=True)
    monkeypatch.setattr(openai_routes.S, "GATEWAY_DEBUG_OPENAI_REQUESTS", True, raising=False)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "continue-local",
                "messages": [{"role": "user", "content": "secret prompt text"}],
                "completionOptions": {
                    "tools": [
                        {
                            "type": "function",
                            "displayTitle": "Read File",
                            "function": {"name": "read_file", "description": "secret description", "parameters": {}},
                        }
                    ]
                },
            },
        )

    assert response.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    request_logs = [message for message in messages if "openai request diagnostics" in message]
    response_logs = [message for message in messages if "openai response diagnostics" in message]
    assert request_logs
    assert response_logs
    assert any('"tools_completion_options_present": true' in message for message in request_logs)
    assert any('"status_code": 200' in message for message in response_logs)
    assert all("secret prompt text" not in message for message in messages)
    assert all("secret description" not in message for message in messages)


def test_chat_completions_returns_diagnostic_500_for_handler_exception(monkeypatch):
    def _stream_handler(_req, _backend: str, _model_name: str):
        raise RuntimeError("boom from test")

    client = _build_client(monkeypatch, backend_supports_tools=True, stream_handler=_stream_handler)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "continue-local",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "500"
    assert "boom from test" in body["error"]["message"]
    assert "request_id=" in body["error"]["message"]
    assert body["error"]["detail"]["request_id"]
