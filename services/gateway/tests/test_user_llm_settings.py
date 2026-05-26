from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import user_llm
from app import ui_routes
from app.agent_runtime_v1 import run_agent_v1
from app.models import AgentRunRequest, AgentSpecModel, ChatCompletionRequest, ChatMessage


def _settings(api_key: str = "sk-test-value") -> dict:
    return {
        "commercial_llms": {
            "enabled": True,
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": api_key,
                    "base_url": "https://api.openai.com/v1",
                    "models": ["gpt-test"],
                }
            },
        }
    }


def test_user_llm_settings_sanitize_hides_keys():
    sanitized = ui_routes._sanitize_user_settings_for_response(_settings("sk-secret-1234"))

    provider = sanitized["commercial_llms"]["providers"]["openai"]
    assert "api_key" not in provider
    assert provider["api_key_configured"] is True
    assert provider["api_key_hint"] == "sk-s...1234"


def test_user_llm_settings_merge_preserves_and_clears_keys():
    current = _settings("sk-existing-1234")
    blank_patch = {
        "commercial_llms": {
            "enabled": True,
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "",
                    "base_url": "https://api.openai.com/v1",
                    "models": "gpt-test, gpt-next",
                }
            },
        }
    }
    merged = ui_routes._merge_user_settings_with_secrets(current, blank_patch)
    provider = merged["commercial_llms"]["providers"]["openai"]
    assert provider["api_key"] == "sk-existing-1234"
    assert provider["models"] == ["gpt-test", "gpt-next"]

    clear_patch = {
        "commercial_llms": {
            "providers": {
                "openai": {
                    "clear_api_key": True,
                }
            }
        }
    }
    cleared = ui_routes._merge_user_settings_with_secrets(merged, clear_patch)
    assert "api_key" not in cleared["commercial_llms"]["providers"]["openai"]


def test_user_llm_model_entries_require_enabled_key_and_models():
    entries = user_llm.model_entries(_settings("sk-test-value"), created=123)

    assert entries == [
        {
            "id": "user_llm:openai:gpt-test",
            "object": "model",
            "created": 123,
            "owned_by": "openai",
            "is_user_llm": True,
            "provider": "openai",
            "upstream_model": "gpt-test",
            "label": "OpenAI: gpt-test (user API key)",
        }
    ]

    disabled = _settings("sk-test-value")
    disabled["commercial_llms"]["enabled"] = False
    assert user_llm.model_entries(disabled, created=123) == []


def test_user_llm_extract_model_ids_accepts_openai_shape():
    assert user_llm.extract_model_ids(
        {
            "object": "list",
            "data": [
                {"id": "gpt-test"},
                {"id": "gpt-next"},
                {"id": "gpt-test"},
                {"name": "fallback-name"},
            ],
        }
    ) == ["gpt-test", "gpt-next", "fallback-name"]


@pytest.mark.asyncio
async def test_user_llm_discovers_provider_models(monkeypatch):
    captured = {}

    class Resp:
        text = '{"data":[{"id":"gpt-test"},{"id":"gpt-next"}]}'
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "gpt-test"}, {"id": "gpt-next"}]}

    class Client:
        async def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return Resp()

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        captured["timeout"] = timeout
        yield Client()

    monkeypatch.setattr(user_llm, "_httpx_client", fake_client)

    models = await user_llm.discover_provider_models(
        provider="openai",
        settings={},
        api_key="sk-test-value",
        base_url="https://api.openai.com/v1",
    )

    assert models == ["gpt-test", "gpt-next"]
    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-value"
    assert captured["timeout"] == 30


@pytest.mark.asyncio
async def test_user_llm_discover_models_rejects_unknown_provider():
    with pytest.raises(HTTPException) as exc:
        await user_llm.discover_provider_models(
            provider="not_a_provider",
            settings={},
            api_key="sk-test-value",
            base_url="https://example.invalid/v1",
        )
    assert getattr(exc.value, "status_code", None) == 400


def test_user_llm_settings_ui_has_key_status_and_model_loading_controls():
    root = os.path.join(os.path.dirname(__file__), "..", "app", "static")
    with open(os.path.join(root, "chat.html"), encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(root, "chat.js"), encoding="utf-8") as f:
        js = f.read()

    assert ".user-llm-provider-card.key-saved" in html
    assert "settings_user_llm_openai_load_models" in html
    assert "settings_user_llm_openrouter_load_models" in html
    assert "settings_user_llm_custom_openai_load_models" in html
    assert "/ui/api/user/llms/models" in js
    assert "loadCommercialLlmModels(provider.id)" in js


def test_ui_model_alias_hides_fetching_models_and_falls_back(monkeypatch):
    fallback = "mlx-community/Phi-4-reasoning-plus-4bit"
    primary = "mlx-community/MiniMax-M2.5-8bit"
    monkeypatch.setattr(ui_routes.S, "MLX_FALLBACK_MODEL", fallback, raising=False)

    class Registry:
        def resolve_backend_class(self, backend):
            return "local_mlx"

    def unavailable(_backend, model):
        return "fetching" if model == primary else None

    monkeypatch.setattr(ui_routes, "model_unavailable_reason", unavailable)
    alias = SimpleNamespace(backend="local_mlx", upstream_model=primary)

    show, display_model, reason = ui_routes._ui_alias_display_model("mlx", alias, Registry())
    assert show is True
    assert display_model == fallback
    assert reason == "fetching"

    show, display_model, reason = ui_routes._ui_alias_display_model("reasoning", alias, Registry())
    assert show is False
    assert display_model == primary
    assert reason == "fetching"


def test_ui_model_alias_hides_embedding_selectors():
    class Registry:
        def resolve_backend_class(self, backend):
            return backend

    alias = SimpleNamespace(backend="local_vllm_embeddings", upstream_model="embedding-model")
    assert ui_routes._ui_alias_is_chat_selector("embeddings", alias, Registry()) is False


@pytest.mark.asyncio
async def test_agent_runtime_uses_user_llm_without_local_router(monkeypatch):
    from app import agent_runtime_v1

    monkeypatch.setattr(
        agent_runtime_v1,
        "load_agent_specs",
        lambda: {
            "default": AgentSpecModel(
                model="user_llm:openai:gpt-test",
                tier=0,
                tools_allowlist=[],
            )
        },
    )
    monkeypatch.setattr(agent_runtime_v1, "_persist_run", lambda run_id, payload: None)

    calls = []

    async def fake_call(req, *, model_id, settings):
        calls.append((req.model, model_id, settings))
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                    }
                }
            ]
        }

    monkeypatch.setattr(agent_runtime_v1.user_llm, "call_user_chat", fake_call)

    class Req:
        headers = {}

    payload, backend, upstream_model = await run_agent_v1(
        req=Req(),  # type: ignore[arg-type]
        run_req=AgentRunRequest(input="hello"),
        user_settings=_settings("sk-test-value"),
    )

    assert backend == "user_llm:openai"
    assert upstream_model == "gpt-test"
    assert payload["ok"] is True
    assert payload["output_text"] == "done"
    assert len(calls) == 2
    assert all(call[1] == "user_llm:openai:gpt-test" for call in calls)


@pytest.mark.asyncio
async def test_user_llm_stream_adapts_non_sse_json(monkeypatch):
    class Resp:
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

        async def aread(self):
            return b'{"choices":[{"message":{"role":"assistant","content":"hello"}}]}'

    class StreamCtx:
        async def __aenter__(self):
            return Resp()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class Client:
        def stream(self, *args, **kwargs):
            return StreamCtx()

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        yield Client()

    monkeypatch.setattr(user_llm, "_httpx_client", fake_client)

    req = ChatCompletionRequest(
        model="user_llm:openai:gpt-test",
        messages=[ChatMessage(role="user", content="hi")],
        stream=True,
    )
    chunks = [chunk async for chunk in user_llm.stream_user_chat_as_openai(req, model_id=req.model, settings=_settings())]

    assert any(b'"delta":{"content":"hello"}' in chunk for chunk in chunks)
    assert chunks[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_ui_stream_reports_empty_upstream_response():
    async def upstream():
        yield b"data: [DONE]\n\n"

    class Admission:
        released = False

        def release(self, backend_class, route_kind):
            self.released = True

    admission = Admission()
    chunks = [
        chunk
        async for chunk in ui_routes._stream_ui_chat(
            upstream(),
            backend="user_llm:openai",
            upstream_model="gpt-test",
            route=SimpleNamespace(reason="user_llm_settings"),
            conversation_id="",
            user=None,
            backend_class="user_llm:openai",
            admission=admission,
        )
    ]

    assert any(b'"type":"empty_response"' in chunk for chunk in chunks)
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert admission.released is True
