from __future__ import annotations

import logging
import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import openai_routes
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
    monkeypatch.setattr(openai_routes, "stream_backend_chat_as_openai", stream_handler or _default_stream_backend_chat)

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
    client = _build_client(monkeypatch, backend_supports_tools=False)

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
