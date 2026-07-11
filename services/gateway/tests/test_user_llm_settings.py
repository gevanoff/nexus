from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import app.httpx_client as httpx_client_module
from app import user_llm
from app import ui_routes
from app import mlx_huge_lane
from app.model_aliases import ModelAlias
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


@pytest.mark.asyncio
async def test_shared_http_client_configures_connect_retries(monkeypatch):
    captured: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured["transport"] = kwargs

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(httpx_client_module.S, "BACKEND_CONNECT_RETRIES", 3, raising=False)
    monkeypatch.setattr(httpx_client_module.S, "BACKEND_VERIFY_TLS", False, raising=False)
    monkeypatch.setattr(httpx_client_module.S, "BACKEND_CA_BUNDLE", "", raising=False)
    monkeypatch.setattr(httpx_client_module.S, "BACKEND_CLIENT_CERT", "", raising=False)
    monkeypatch.setattr(httpx_client_module.httpx, "AsyncHTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx_client_module.httpx, "AsyncClient", FakeClient)

    async with httpx_client_module.httpx_client(timeout=12) as client:
        assert isinstance(client, FakeClient)

    assert captured["transport"] == {"retries": 3, "verify": False}
    assert captured["client"]["timeout"] == 12
    assert isinstance(captured["client"]["transport"], FakeTransport)


@pytest.mark.asyncio
async def test_ui_model_catalogs_exposes_shared_coding_catalog(monkeypatch):
    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda _req: None)
    monkeypatch.setattr(ui_routes, "_require_user", lambda _req: SimpleNamespace(id=1))
    monkeypatch.setattr(
        ui_routes.coding_model_policy,
        "options_payload",
        lambda: {"source": "model_aliases", "options": [{"value": "coder"}]},
    )

    payload = await ui_routes.ui_model_catalogs(SimpleNamespace())

    assert payload["coding"]["source"] == "model_aliases"
    assert payload["coding"]["options"] == [{"value": "coder"}]


@pytest.mark.asyncio
async def test_ui_model_defaults_exposes_shared_defaults(monkeypatch):
    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda _req: None)
    monkeypatch.setattr(ui_routes, "_require_user", lambda _req: SimpleNamespace(id=1))
    monkeypatch.setattr(ui_routes, "_ui_chat_model_preference_for_user", lambda _user: "default")
    monkeypatch.setattr(ui_routes, "_ui_coding_model_preference_for_user", lambda _user: "coder")
    monkeypatch.setattr(
        ui_routes,
        "_task_type_capabilities",
        lambda: [
            {"id": "llm", "enabled": True, "default_model": "default"},
            {"id": "coder", "enabled": True, "default_model": "coder"},
            {"id": "image", "enabled": False, "default_model": "image"},
        ],
    )

    payload = await ui_routes.ui_model_defaults(SimpleNamespace())

    assert payload["source"] == "gateway_model_defaults.v1"
    assert payload["chat"]["model"] == "default"
    assert payload["coding"]["model"] == "coder"
    assert payload["scheduled_tasks"] == {"llm": "default", "coder": "coder"}


def test_user_llm_settings_sanitize_hides_keys():
    sanitized = ui_routes._sanitize_user_settings_for_response(_settings("sk-secret-1234"))

    provider = sanitized["commercial_llms"]["providers"]["openai"]
    assert "api_key" not in provider
    assert provider["api_key_configured"] is True
    assert provider["api_key_hint"] == "sk-s...1234"
    assert provider["available_models"] == ["gpt-test"]


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
    assert provider["available_models"] == ["gpt-test", "gpt-next"]

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


def test_user_settings_merge_normalizes_stale_coding_model_preference(monkeypatch):
    monkeypatch.setattr(
        ui_routes.coding_model_policy,
        "normalize_preferred_coding_model",
        lambda value: "coder" if value == "mlx-community/GLM-5-4bit" else value,
    )
    current = {
        "coding": {
            "model_preference": "coder",
        }
    }
    patch = {
        "coding": {
            "model_preference": "mlx-community/GLM-5-4bit",
        }
    }

    merged = ui_routes._merge_user_settings_with_secrets(current, patch)

    assert merged["coding"]["model_preference"] == "coder"


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
    with open(os.path.join(root, "admin_models.html"), encoding="utf-8") as f:
        admin_models_html = f.read()
    with open(os.path.join(root, "admin_models.js"), encoding="utf-8") as f:
        admin_models_js = f.read()
    with open(os.path.join(root, "resources.html"), encoding="utf-8") as f:
        resources_html = f.read()
    with open(os.path.join(root, "resources.js"), encoding="utf-8") as f:
        resources_js = f.read()

    assert ".user-llm-provider-card.key-saved" in html
    assert "settings_user_llm_openai_load_models" in html
    assert "settings_user_llm_openrouter_load_models" in html
    assert "settings_user_llm_custom_openai_load_models" in html
    assert "/ui/api/user/llms/models" in js
    assert "loadCommercialLlmModels(provider.id)" in js
    assert "settings_user_llm_openai_models_list" in html
    assert "settings_user_llm_openai_select_all_models" in html
    assert "available_models" in js
    assert "Session expired. Redirecting to sign in..." in js
    assert "window.location.replace(`/ui/login?next=${back}`)" in js
    assert "settings_logout" in html
    assert '/static/chat.js?v=18' in html
    assert 'id="loadModels"' not in html
    assert "/ui/api/auth/logout" in js
    assert "resolveRequestedChatModel" in js
    assert "settings-models" not in html
    assert "/ui/admin/models" in html
    assert "Nexus model admin" in admin_models_html
    assert "/ui/api/admin/models" in admin_models_js
    assert "/ui/api/admin/models/benchmark" in admin_models_js
    assert "/ui/api/admin/models/tool-qualification" in admin_models_js
    assert "/ui/api/admin/models/prefetch" in admin_models_js
    assert "Start benchmark" in admin_models_html
    assert "Run tool suite" in admin_models_html
    assert "benchmarkText" in admin_models_js
    assert "benchmark_latest" in admin_models_js
    assert "toolQualificationText" in admin_models_js
    assert "tool_qualification_latest" in admin_models_js
    assert "Restart fetch" in admin_models_js
    assert "Re-download" in admin_models_js
    assert "Purge cache" in admin_models_js
    assert "re-download in progress" in admin_models_js
    assert "purge requested" in admin_models_js
    assert "/ui/api/admin/models/purge" in admin_models_js
    assert "/ui/api/admin/models/redownload" in admin_models_js
    assert "mlx_huge_lane" not in resources_html
    assert "/ui/api/mlx/huge-lane/switch" not in resources_js
    assert "MLX Huge Model" in admin_models_js
    assert "/ui/api/mlx/huge-lane/switch" in admin_models_js
    assert "not resident-compatible" in admin_models_js
    assert "admin_models.js?v=12" in admin_models_html


def test_canonical_chat_aliases_match_runtime_lanes():
    aliases_path = os.path.join(os.path.dirname(__file__), "..", "app", "model_aliases.json")
    with open(aliases_path, encoding="utf-8") as f:
        payload = json.load(f)

    aliases = payload["aliases"]
    strong_model = "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic"
    fast_model = "cyankiwi/Devstral-Small-2507-AWQ-4bit"
    mlx_model = "mlx-community/GLM-5.2-DQ4plus-q8"

    assert aliases["default"]["backend"] == "local_vllm"
    assert aliases["default"]["model"] == strong_model
    assert aliases["default"]["context_window"] == 8192
    assert aliases["default"]["max_tokens_cap"] == 1024
    assert aliases["coder"]["backend"] == "local_mlx"
    assert aliases["coder"]["model"] == mlx_model
    assert aliases["fast"]["backend"] == "local_vllm_fast"
    assert aliases["fast"]["model"] == fast_model
    assert aliases["fast"]["context_window"] == 8192
    assert aliases["fast"]["tools"] is False
    assert aliases["fast"]["max_tokens_cap"] == 768
    assert aliases["fast-reasoning"]["backend"] == "local_vllm"
    assert aliases["fast-reasoning"]["model"] == strong_model
    assert aliases["fast-reasoning"]["context_window"] == 32768
    assert aliases["fast-reasoning"]["max_tokens_cap"] == 2048
    assert aliases["long"]["context_window"] == 65536
    assert aliases["long"]["tools"] is True
    assert aliases["reasoning"]["backend"] == "local_vllm"
    assert aliases["reasoning"]["model"] == strong_model
    assert aliases["reasoning"]["context_window"] == 8192
    assert aliases["reasoning"]["max_tokens_cap"] == 1024
    assert aliases["glm-5.2"]["huge_candidate"] is True
    assert aliases["glm-5.2"]["huge_default"] is True
    assert aliases["glm-5.2-mxfp4"]["context_window"] == 32768
    assert aliases["glm-5.2-mxfp4"]["max_tokens_cap"] == 2048
    assert aliases["minimax-m3"]["model"] == "mlx-community/MiniMax-M3-4bit"
    assert aliases["minimax-m3"]["huge_switchable"] is False
    assert aliases["minimax-m3"]["context_window"] == 32768
    assert aliases["minimax-m3"]["max_tokens_cap"] == 2048
    assert aliases["deepseek-r1"]["model"] == "mlx-community/DeepSeek-R1-0528-4bit"
    assert aliases["deepseek-r1"]["context_window"] == 32768
    assert aliases["deepseek-r1"]["max_tokens_cap"] == 2048


def test_mlx_huge_lane_state_keeps_requests_on_confirmed_resident(monkeypatch, tmp_path):
    state_path = tmp_path / "mlx_huge_lane.json"
    monkeypatch.setattr(mlx_huge_lane.S, "MLX_HUGE_LANE_STATE_PATH", str(state_path), raising=False)
    monkeypatch.setattr(mlx_huge_lane.S, "MLX_HUGE_LANE_ENABLED", True, raising=False)
    aliases = {
        "huge-a": ModelAlias(
            backend="local_mlx", upstream_model="model-a", huge_candidate=True, huge_default=True
        ),
        "huge-b": ModelAlias(backend="local_mlx", upstream_model="model-b", huge_candidate=True),
    }
    monkeypatch.setattr(mlx_huge_lane, "get_aliases", lambda: aliases)

    assert mlx_huge_lane.load_state()["active_model"] == "model-a"
    assert [item["alias"] for item in mlx_huge_lane.load_state()["candidates"]] == ["huge-a", "huge-b"]

    switching = mlx_huge_lane.mark_switching("model-b")
    assert switching["active_model"] == "model-a"
    assert switching["target_model"] == "model-b"
    assert switching["route_model"] == "model-a"
    assert mlx_huge_lane.route_model() == "model-a"
    block = mlx_huge_lane.request_block("model-a")
    assert block["error"] == "mlx_huge_transition_in_progress"
    assert block["target_model"] == "model-b"

    ready = mlx_huge_lane.mark_ready("model-b")
    assert ready["active_model"] == "model-b"
    assert ready["target_model"] == ""
    assert ready["route_model"] == "model-b"
    assert mlx_huge_lane.request_block("model-a")["error"] == "mlx_huge_model_not_resident"
    assert mlx_huge_lane.request_block("model-b") is None


def test_user_llm_available_models_include_selected_models():
    settings = {
        "commercial_llms": {
            "enabled": True,
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "sk-test",
                    "base_url": "https://api.openai.com/v1",
                    "models": ["gpt-picked"],
                    "available_models": ["gpt-other"],
                }
            },
        }
    }

    assert user_llm.available_models(settings, "openai") == ["gpt-other", "gpt-picked"]


def test_ui_model_alias_hides_fetching_models_and_falls_back(monkeypatch):
    fallback = "unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M"
    primary = "mlx-community/MiniMax-M3-4bit"
    monkeypatch.setattr(ui_routes.S, "MLX_FALLBACK_BACKEND", "local_vllm_fast", raising=False)
    monkeypatch.setattr(ui_routes.S, "MLX_FALLBACK_MODEL", fallback, raising=False)

    class Registry:
        def resolve_backend_class(self, backend):
            return "local_mlx"

    def unavailable(_backend, model):
        return "fetching" if model == primary else None

    monkeypatch.setattr(ui_routes, "model_unavailable_reason", unavailable)
    monkeypatch.setattr(ui_routes.mlx_huge_lane, "route_model", lambda: "")
    alias = SimpleNamespace(backend="local_mlx", upstream_model=primary)

    show, display_model, reason = ui_routes._ui_alias_display_model("mlx", alias, Registry())
    assert show is True
    assert display_model == fallback
    assert reason == "fetching"

    show, display_model, reason = ui_routes._ui_alias_display_model("reasoning", alias, Registry())
    assert show is False
    assert display_model == primary
    assert reason == "fetching"


def test_ui_model_alias_does_not_treat_advertised_fetching_model_as_available(monkeypatch):
    primary = "mlx-community/DeepSeek-R1-0528-4bit"

    class Registry:
        def resolve_backend_class(self, backend):
            return "local_mlx"

    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda *_args, **_kwargs: "fetching")
    alias = SimpleNamespace(backend="local_mlx", upstream_model=primary)

    show, display_model, reason = ui_routes._ui_alias_display_model(
        "reasoning",
        alias,
        Registry(),
        advertised_models_by_backend={"local_mlx": {primary}},
    )

    assert show is False
    assert display_model == primary
    assert reason == "fetching"


def test_ui_model_alias_treats_advertised_missing_model_as_available(monkeypatch):
    primary = "mlx-community/GLM-5.2-DQ4plus-q8"

    class Registry:
        def resolve_backend_class(self, backend):
            return "local_mlx"

    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda *_args, **_kwargs: "missing")
    alias = SimpleNamespace(backend="local_mlx", upstream_model=primary)

    show, display_model, reason = ui_routes._ui_alias_display_model(
        "long",
        alias,
        Registry(),
        advertised_models_by_backend={"local_mlx": {primary}},
    )

    assert show is True
    assert display_model == primary
    assert reason is None


def test_ui_runtime_selector_does_not_treat_advertised_fetching_model_as_available(monkeypatch):
    primary = "mlx-community/MiniMax-M3-4bit"

    class Backend:
        base_url = "http://ai2:10240/v1"

    class Registry:
        def resolve_backend_class(self, backend):
            return "local_mlx" if backend == "mlx" else backend

        def get_backend(self, backend):
            return Backend() if backend == "local_mlx" else None

    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda *_args, **_kwargs: "fetching")
    monkeypatch.setattr(ui_routes, "fallback_target_for_backend", lambda _backend: None)
    monkeypatch.setattr(ui_routes, "_backend_location_details", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ui_routes.S, "MLX_MODEL_STRONG", primary, raising=False)

    entries = ui_routes._ui_runtime_selector_entries(
        Registry(),
        123,
        advertised_models_by_backend={"local_mlx": {primary}},
    )

    assert entries == []


def test_ui_runtime_selector_falls_back_to_advertised_model_id(monkeypatch):
    served = "unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M"

    class Backend:
        base_url = "http://stackrot:8001/v1"

    class Registry:
        def resolve_backend_class(self, backend):
            return "local_vllm_fast" if backend == "vllm_fast" else backend

        def get_backend(self, backend):
            return Backend() if backend == "local_vllm_fast" else None

    monkeypatch.setattr(ui_routes, "_backend_location_details", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ui_routes.S, "VLLM_MODEL_FAST", "/root/models/qwen-fast.gguf", raising=False)

    entries = ui_routes._ui_runtime_selector_entries(
        Registry(),
        123,
        advertised_models_by_backend={"local_vllm_fast": {served}},
    )

    assert [entry["id"] for entry in entries] == ["vllm_fast"]
    assert entries[0]["resolved_model"] == served


def test_ui_advertised_models_by_backend_collects_probe_items():
    items = [
        {"backend": "local_mlx", "upstream_model": "model-a"},
        {"backend": "local_mlx", "upstream_model": "model-b"},
        {"backend": "local_vllm", "upstream_model": "model-c"},
        {"backend": "", "upstream_model": "ignored"},
    ]

    advertised = ui_routes._ui_advertised_models_by_backend(items)

    assert advertised == {
        "local_mlx": {"model-a", "model-b"},
        "local_vllm": {"model-c"},
    }


@pytest.mark.asyncio
async def test_ui_models_includes_probed_backend_models(monkeypatch):
    class Backend:
        base_url = "http://ai2:10240/v1"

    class Registry:
        def resolve_backend_class(self, backend):
            return backend

        def get_backend(self, backend):
            return Backend() if backend == "local_mlx" else None

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        yield object()

    async def fake_probe(_client, _registry, backend_name, _base_url, now):
        return (
            [
                {
                    "id": "local_mlx:model-a",
                    "object": "model",
                    "created": now,
                    "owned_by": "local",
                    "backend": backend_name,
                    "upstream_model": "model-a",
                }
            ],
            {"backend": backend_name, "ok": True, "count": 1},
        )

    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda _req: None)
    monkeypatch.setattr(ui_routes, "_require_user", lambda _req: SimpleNamespace(id=1))
    monkeypatch.setattr(ui_routes, "now_unix", lambda: 123)
    monkeypatch.setattr(ui_routes, "_ui_models_cache_ttl_sec", lambda: 0)
    monkeypatch.setattr(ui_routes, "_ui_models_probe_timeout_sec", lambda: 1)
    monkeypatch.setattr(ui_routes, "_httpx_client", fake_client)
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "llm_backends", lambda: [("local_mlx", Backend())])
    monkeypatch.setattr(ui_routes, "_probe_models_for_backend", fake_probe)
    monkeypatch.setattr(ui_routes, "get_aliases", lambda: {})
    monkeypatch.setattr(ui_routes, "get_health_checker", lambda: SimpleNamespace(get_status=lambda _name: None))
    monkeypatch.setattr(ui_routes, "_settings_for_optional_user", lambda _user: {})
    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda _backend, _model: None)
    monkeypatch.setattr(ui_routes.user_llm, "model_entries", lambda _settings, created: [])

    payload = await ui_routes.ui_models(SimpleNamespace())

    assert [item["id"] for item in payload["data"]] == ["local_mlx:model-a"]


@pytest.mark.asyncio
async def test_ui_models_hides_fetching_probed_backend_models(monkeypatch):
    class Backend:
        base_url = "http://ai2:10240/v1"

    class Registry:
        def resolve_backend_class(self, backend):
            return backend

        def get_backend(self, backend):
            return Backend() if backend == "local_mlx" else None

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        yield object()

    async def fake_probe(_client, _registry, backend_name, _base_url, now):
        return (
            [
                {
                    "id": "local_mlx:mlx-community/MiniMax-M3-4bit",
                    "object": "model",
                    "created": now,
                    "owned_by": "local",
                    "backend": backend_name,
                    "upstream_model": "mlx-community/MiniMax-M3-4bit",
                }
            ],
            {"backend": backend_name, "ok": True, "count": 1},
        )

    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda _req: None)
    monkeypatch.setattr(ui_routes, "_require_user", lambda _req: SimpleNamespace(id=1))
    monkeypatch.setattr(ui_routes, "now_unix", lambda: 123)
    monkeypatch.setattr(ui_routes, "_ui_models_cache_ttl_sec", lambda: 0)
    monkeypatch.setattr(ui_routes, "_ui_models_probe_timeout_sec", lambda: 1)
    monkeypatch.setattr(ui_routes, "_httpx_client", fake_client)
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "llm_backends", lambda: [("local_mlx", Backend())])
    monkeypatch.setattr(ui_routes, "_probe_models_for_backend", fake_probe)
    monkeypatch.setattr(ui_routes, "get_aliases", lambda: {})
    monkeypatch.setattr(ui_routes, "get_health_checker", lambda: SimpleNamespace(get_status=lambda _name: None))
    monkeypatch.setattr(ui_routes, "_settings_for_optional_user", lambda _user: {})
    monkeypatch.setattr(ui_routes.user_llm, "model_entries", lambda _settings, created: [])
    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda _backend, _model: "fetching")

    payload = await ui_routes.ui_models(SimpleNamespace())

    assert payload["data"] == []
    assert payload["diagnostics"]["sources"]["hidden_probed_models"]["local_mlx:mlx-community/MiniMax-M3-4bit"]["reason"] == "mlx_huge_lane_controlled"


@pytest.mark.asyncio
async def test_admin_models_reports_alias_effective_fallback(monkeypatch):
    class Backend:
        base_url = "http://ai2:10240/v1"

    class Registry:
        def resolve_backend_class(self, backend):
            return backend

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        yield object()

    async def fake_probe(_client, _registry, backend_name, _base_url, now):
        return (
            [
                {
                    "id": "local_mlx:mlx-community/MiniMax-M3-4bit",
                    "object": "model",
                    "created": now,
                    "owned_by": "local",
                    "backend": backend_name,
                    "upstream_model": "mlx-community/MiniMax-M3-4bit",
                }
            ],
            {"backend": backend_name, "ok": True, "count": 1},
        )

    monkeypatch.setattr(ui_routes, "_require_admin", lambda _req: SimpleNamespace(id=1, admin=True))
    monkeypatch.setattr(ui_routes, "now_unix", lambda: 123)
    monkeypatch.setattr(ui_routes, "_ui_models_probe_timeout_sec", lambda: 1)
    monkeypatch.setattr(ui_routes, "_httpx_client", fake_client)
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "llm_backends", lambda: [("local_mlx", Backend())])
    monkeypatch.setattr(ui_routes, "_probe_models_for_backend", fake_probe)
    monkeypatch.setattr(ui_routes, "get_aliases", lambda: {"default": SimpleNamespace(backend="local_mlx", upstream_model="mlx-community/MiniMax-M3-4bit", tools=True, context_window=None, max_tokens_cap=None, temperature_cap=None)})
    monkeypatch.setattr(ui_routes, "get_aliases_state", lambda: SimpleNamespace(source="test", configured_path="", error=""))
    monkeypatch.setattr(ui_routes, "get_health_checker", lambda: SimpleNamespace(get_status=lambda _name: None))
    monkeypatch.setattr(ui_routes, "backend_hostname", lambda *_args, **_kwargs: "ai2")
    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda backend, model: "fetching" if backend == "local_mlx" and model.endswith("MiniMax-M3-4bit") else None)
    monkeypatch.setattr(ui_routes, "fallback_target_for_backend", lambda backend: ("local_vllm_fast", "fast-model") if backend == "local_mlx" else None)
    monkeypatch.setattr(ui_routes, "hf_model_cache_state", lambda model: "fetching" if model.endswith("MiniMax-M3-4bit") else None)
    monkeypatch.setattr(
        ui_routes,
        "hf_model_cache_details",
        lambda model: {
            "state": "fetching" if model.endswith("MiniMax-M3-4bit") else None,
            "fetch_activity": {"status": "stalled", "last_progress_age_sec": 999},
        },
    )
    monkeypatch.setattr(ui_routes, "hf_model_cache_entries", lambda: {})
    monkeypatch.setattr(
        ui_routes.model_tool_qualification,
        "latest_by_model",
        lambda: {
            "default": {
                "ok": False,
                "passed": 3,
                "total": 7,
                "first_error": "auto failed",
                "by_category": {"auto": {"passed": 0, "total": 2}},
            },
            "local_vllm_fast:fast-model": {
                "ok": True,
                "passed": 7,
                "total": 7,
                "first_error": "",
                "by_category": {"auto": {"passed": 2, "total": 2}},
            },
        },
    )

    payload = await ui_routes.ui_admin_models(SimpleNamespace())

    assert payload["aliases"][0]["effective_backend"] == "local_vllm_fast"
    assert payload["aliases"][0]["effective_model"] == "fast-model"
    assert payload["aliases"][0]["tool_qualification_latest"]["ok"] is True
    minimax = next(item for item in payload["models"] if item["model"].endswith("MiniMax-M3-4bit"))
    assert minimax["unavailable_reason"] == "fetching"
    assert minimax["fetch_activity"]["status"] == "stalled"
    fast_model = next(item for item in payload["models"] if item["model"] == "fast-model")
    assert fast_model["tool_qualification_latest"]["ok"] is True


@pytest.mark.asyncio
async def test_admin_models_treats_advertised_missing_mlx_model_as_selectable(monkeypatch):
    class Backend:
        base_url = "http://ai2:10240/v1"

    class Registry:
        def resolve_backend_class(self, backend):
            return backend

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        yield object()

    model_id = "mlx-community/GLM-5.2-DQ4plus-q8"

    async def fake_probe(_client, _registry, backend_name, _base_url, now):
        return (
            [
                {
                    "id": f"{backend_name}:{model_id}",
                    "object": "model",
                    "created": now,
                    "owned_by": "local",
                    "backend": backend_name,
                    "upstream_model": model_id,
                }
            ],
            {"backend": backend_name, "ok": True, "count": 1},
        )

    monkeypatch.setattr(ui_routes, "_require_admin", lambda _req: SimpleNamespace(id=1, admin=True))
    monkeypatch.setattr(ui_routes, "now_unix", lambda: 123)
    monkeypatch.setattr(ui_routes, "_ui_models_probe_timeout_sec", lambda: 1)
    monkeypatch.setattr(ui_routes, "_httpx_client", fake_client)
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "llm_backends", lambda: [("local_mlx", Backend())])
    monkeypatch.setattr(ui_routes, "_probe_models_for_backend", fake_probe)
    monkeypatch.setattr(ui_routes, "get_aliases", lambda: {})
    monkeypatch.setattr(ui_routes, "get_aliases_state", lambda: SimpleNamespace(source="test", configured_path="", error=""))
    monkeypatch.setattr(ui_routes, "get_health_checker", lambda: SimpleNamespace(get_status=lambda _name: None))
    monkeypatch.setattr(ui_routes, "backend_hostname", lambda *_args, **_kwargs: "ai2")
    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda _backend, _model: "missing")
    monkeypatch.setattr(ui_routes, "hf_model_cache_state", lambda _model: "missing")
    monkeypatch.setattr(ui_routes, "hf_model_cache_details", lambda _model: {"state": "missing", "fetch_activity": None})
    monkeypatch.setattr(ui_routes, "hf_model_cache_entries", lambda: {})

    payload = await ui_routes.ui_admin_models(SimpleNamespace())

    row = next(item for item in payload["models"] if item["model"] == model_id)
    assert row["advertised"] is True
    assert row["unavailable_reason"] is None
    assert row["selectable"] is True


@pytest.mark.asyncio
async def test_ui_admin_models_marks_cached_unadvertised_mlx_models_not_selectable(monkeypatch):
    class Backend:
        base_url = "http://mlx"

    class Registry:
        def resolve_backend_class(self, backend):
            return backend

        def get_backend(self, backend):
            return Backend()

    @asynccontextmanager
    async def fake_client(*, timeout=None):
        yield object()

    async def fake_probe(_client, _registry, backend_name, _base_url, now):
        return ([], {"backend": backend_name, "ok": True, "count": 0})

    model_id = "mlx-community/GLM-5-8bit-MXFP8"

    monkeypatch.setattr(ui_routes, "_require_admin", lambda _req: SimpleNamespace(id=1, admin=True))
    monkeypatch.setattr(ui_routes, "now_unix", lambda: 123)
    monkeypatch.setattr(ui_routes, "_ui_models_probe_timeout_sec", lambda: 1)
    monkeypatch.setattr(ui_routes, "_httpx_client", fake_client)
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "llm_backends", lambda: [("local_mlx", Backend())])
    monkeypatch.setattr(ui_routes, "_probe_models_for_backend", fake_probe)
    monkeypatch.setattr(ui_routes, "get_aliases", lambda: {})
    monkeypatch.setattr(ui_routes, "get_aliases_state", lambda: SimpleNamespace(source="test", configured_path="", error=""))
    monkeypatch.setattr(ui_routes, "get_health_checker", lambda: SimpleNamespace(get_status=lambda _name: None))
    monkeypatch.setattr(ui_routes, "backend_hostname", lambda *_args, **_kwargs: "ai2")
    monkeypatch.setattr(ui_routes, "model_unavailable_reason", lambda _backend, _model: None)
    monkeypatch.setattr(ui_routes, "hf_model_cache_state", lambda model: "cached" if model == model_id else None)
    monkeypatch.setattr(ui_routes, "hf_model_cache_details", lambda model: {"state": "cached" if model == model_id else None, "fetch_activity": None})
    monkeypatch.setattr(ui_routes, "hf_model_cache_entries", lambda: {model_id: "cached"})

    payload = await ui_routes.ui_admin_models(SimpleNamespace())

    row = next(item for item in payload["models"] if item["model"] == model_id)
    assert row["cache_state"] == "cached"
    assert row["cache_only"] is True
    assert row["advertised"] is False
    assert row["unavailable_reason"] == "not_advertised"
    assert row["selectable"] is False


@pytest.mark.asyncio
async def test_ui_admin_model_prefetch_calls_lifecycle_manager(monkeypatch):
    class Registry:
        def resolve_backend_class(self, backend):
            return backend

    calls = []

    async def fake_lifecycle(method, path, *, json_body=None, timeout=None):
        calls.append((method, path, json_body))
        return {"ok": True, "decision": "prefetch_started"}

    monkeypatch.setattr(ui_routes, "_require_admin", lambda _req: SimpleNamespace(id=1, admin=True))
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "backend_provider_name", lambda backend: "mlx" if backend == "local_mlx" else "vllm")
    monkeypatch.setattr(ui_routes, "call_lifecycle_manager", fake_lifecycle)
    monkeypatch.setattr(ui_routes, "lifecycle_timeout", lambda: 3.0)

    payload = await ui_routes.ui_admin_model_prefetch(
        SimpleNamespace(),
        ui_routes.ModelPrefetchRequest(backend="local_mlx", model="mlx-community/MiniMax-M3-4bit"),
    )

    assert payload["decision"] == "prefetch_started"
    assert calls == [
        (
            "POST",
            "/v1/lifecycle/mlx/prefetch",
            {"backend_class": "local_mlx", "model": "mlx-community/MiniMax-M3-4bit"},
        )
    ]


@pytest.mark.asyncio
async def test_ui_admin_model_purge_calls_cache_helper(monkeypatch):
    class Registry:
        def resolve_backend_class(self, backend):
            return backend

    calls = []

    async def fake_lifecycle(method, path, *, json_body=None, timeout=None):
        calls.append((method, path, json_body, timeout))
        return {
            "ok": True,
            "decision": "cache_purged",
            "model": json_body["model"],
            "removed_paths": ["/ai-data/huggingface/models--mlx-community--MiniMax-M3-4bit"],
        }

    async def fake_sync():
        return None

    monkeypatch.setattr(ui_routes, "_require_admin", lambda _req: SimpleNamespace(id=1, admin=True))
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "backend_provider_name", lambda backend: "mlx" if backend == "local_mlx" else "vllm")
    monkeypatch.setattr(ui_routes, "call_lifecycle_manager", fake_lifecycle)
    monkeypatch.setattr(ui_routes, "lifecycle_timeout", lambda: 3.0)
    monkeypatch.setattr(ui_routes, "hf_model_cache_state", lambda model: "missing" if model == "mlx-community/MiniMax-M3-4bit" else None)
    monkeypatch.setattr(ui_routes, "hf_model_cache_details", lambda model: {"state": "missing", "fetch_activity": None})
    monkeypatch.setattr(ui_routes, "_sync_mlx_cache_status_best_effort", fake_sync)

    payload = await ui_routes.ui_admin_model_purge(
        SimpleNamespace(),
        ui_routes.ModelPurgeRequest(backend="local_mlx", model="mlx-community/MiniMax-M3-4bit"),
    )

    assert payload["backend"] == "local_mlx"
    assert payload["cache_state"] == "missing"
    assert payload["removed_paths"]
    assert calls == [
        (
            "POST",
            "/v1/lifecycle/mlx/cache/purge",
            {"backend_class": "local_mlx", "model": "mlx-community/MiniMax-M3-4bit"},
            600.0,
        )
    ]


@pytest.mark.asyncio
async def test_ui_admin_model_redownload_calls_lifecycle_manager(monkeypatch):
    class Registry:
        def resolve_backend_class(self, backend):
            return backend

    calls = []

    async def fake_lifecycle(method, path, *, json_body=None, timeout=None):
        calls.append((method, path, json_body, timeout))
        return {"ok": True, "decision": "redownload_started", "pid": "1234"}

    async def fake_sync():
        return None

    monkeypatch.setattr(ui_routes, "_require_admin", lambda _req: SimpleNamespace(id=1, admin=True))
    monkeypatch.setattr(ui_routes, "get_registry", lambda: Registry())
    monkeypatch.setattr(ui_routes, "backend_provider_name", lambda backend: "mlx" if backend == "local_mlx" else "vllm")
    monkeypatch.setattr(ui_routes, "call_lifecycle_manager", fake_lifecycle)
    monkeypatch.setattr(ui_routes, "lifecycle_timeout", lambda: 3.0)
    monkeypatch.setattr(ui_routes, "hf_model_cache_state", lambda _model: "fetching")
    monkeypatch.setattr(ui_routes, "hf_model_cache_details", lambda _model: {"state": "fetching", "fetch_activity": {"status": "active"}})
    monkeypatch.setattr(ui_routes, "_sync_mlx_cache_status_best_effort", fake_sync)

    payload = await ui_routes.ui_admin_model_redownload(
        SimpleNamespace(),
        ui_routes.ModelPurgeRequest(backend="local_mlx", model="mlx-community/GLM-5.2-DQ4plus-q8"),
    )

    assert payload["decision"] == "redownload_started"
    assert payload["cache_state"] == "fetching"
    assert calls == [
        (
            "POST",
            "/v1/lifecycle/mlx/cache/redownload",
            {"backend_class": "local_mlx", "model": "mlx-community/GLM-5.2-DQ4plus-q8"},
            600.0,
        )
    ]


@pytest.mark.asyncio
async def test_huge_lane_switch_uses_lifecycle_resident_operation(monkeypatch):
    calls = []
    ready = []

    async def fake_lifecycle(method, path, *, json_body=None, timeout=None):
        calls.append((method, path, json_body, timeout))
        return {"ok": True, "decision": "resident_huge_model_ready"}

    monkeypatch.setattr(ui_routes, "call_lifecycle_manager", fake_lifecycle)
    monkeypatch.setattr(ui_routes.S, "MLX_HUGE_LANE_SWITCH_TIMEOUT_SEC", 3900.0, raising=False)
    monkeypatch.setattr(ui_routes.mlx_huge_lane, "mark_ready", lambda model, message="": ready.append((model, message)))
    monkeypatch.setattr(ui_routes, "_clear_ui_models_cache", lambda: None)

    await ui_routes._switch_mlx_huge_lane_model("mlx-community/GLM-5.2-DQ4plus-q8")

    assert calls == [
        (
            "POST",
            "/v1/lifecycle/mlx/huge-lane/switch",
            {"backend_class": "local_mlx", "model": "mlx-community/GLM-5.2-DQ4plus-q8"},
            3900.0,
        )
    ]
    assert ready == [
        ("mlx-community/GLM-5.2-DQ4plus-q8", "mlx-community/GLM-5.2-DQ4plus-q8 is resident")
    ]


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
        calls.append((req.model, model_id, settings, req.tool_choice, bool(req.tools)))
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
    assert calls[0][3:] == (None, False)
    assert calls[1][3:] == ("auto", True)


@pytest.mark.asyncio
async def test_user_llm_stream_adapts_non_sse_json(monkeypatch):
    class Resp:
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

        async def aread(self):
            return b'{"output":[{"type":"message","content":[{"type":"output_text","text":"hello"}]}]}'

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
async def test_user_llm_stream_adapts_response_output_text_sse(monkeypatch):
    class Resp:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in (
                'data: {"type":"response.output_text.delta","delta":"hello"}',
                'data: [DONE]',
            ):
                yield line

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
async def test_user_llm_stream_reports_http_error_when_stream_body_not_pre_read(monkeypatch):
    class Resp:
        status_code = 401
        encoding = None

        async def aread(self):
            return b'{"error":{"message":"bad api key"}}'

        @property
        def text(self):
            raise httpx.ResponseNotRead()

        def raise_for_status(self):
            request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            raise httpx.HTTPStatusError("boom", request=request, response=self)

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

    assert any(b'"type":"upstream_error"' in chunk for chunk in chunks)
    assert any(b'bad api key' in chunk for chunk in chunks)
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


@pytest.mark.asyncio
async def test_ui_stream_handles_missing_admission_controller():
    async def upstream():
        yield b"data: [DONE]\n\n"

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
            admission=None,
        )
    ]

    assert any(b'"type":"empty_response"' in chunk for chunk in chunks)
    assert chunks[-1] == b"data: [DONE]\n\n"
