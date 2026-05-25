from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import user_llm
from app import ui_routes
from app.agent_runtime_v1 import run_agent_v1
from app.models import AgentRunRequest, AgentSpecModel


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
