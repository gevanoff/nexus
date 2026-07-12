from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import upstreams
from app.models import ChatCompletionRequest, ChatMessage


class FakeRegistry:
    def resolve_backend_class(self, backend_name: str) -> str:
        return backend_name

    def get_backend(self, backend_name: str):
        return SimpleNamespace(base_url="http://backend.invalid/v1")


def _alias(*, backend: str, upstream_model: str, cap: int) -> SimpleNamespace:
    return SimpleNamespace(backend=backend, upstream_model=upstream_model, max_tokens_cap=cap)


def _request(model: str = "long", max_tokens: int | None = None) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="Reply OK only.")],
        max_tokens=max_tokens,
        stream=True,
    )


def _patch_backend(
    monkeypatch,
    *,
    provider: str = "mlx",
    cap: int = 64,
    aliases: dict[str, SimpleNamespace] | None = None,
) -> None:
    if aliases is None:
        aliases = {
            "long": _alias(
                backend="local_mlx" if provider == "mlx" else "local_vllm",
                upstream_model="mlx-community/GLM-5.2-4bit" if provider == "mlx" else "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
                cap=cap,
            )
        }

    monkeypatch.setattr(upstreams, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda backend_name: provider)
    monkeypatch.setattr(upstreams, "_enforce_mlx_glm_input_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(upstreams, "get_alias", lambda name: aliases.get(name))
    monkeypatch.setattr(upstreams, "get_aliases", lambda: aliases)


def test_route_request_for_backend_defaults_missing_max_tokens_to_mlx_alias_cap(monkeypatch):
    _patch_backend(monkeypatch, provider="mlx", cap=64)

    routed = upstreams.route_request_for_backend(
        _request(max_tokens=None),
        "local_mlx",
        "mlx-community/GLM-5.2-4bit",
    )

    assert routed.model == "mlx-community/GLM-5.2-4bit"
    assert routed.max_tokens == 64


def test_route_request_for_backend_caps_oversized_max_tokens(monkeypatch):
    _patch_backend(monkeypatch, provider="mlx", cap=64)

    routed = upstreams.route_request_for_backend(
        _request(max_tokens=1024),
        "local_mlx",
        "mlx-community/GLM-5.2-4bit",
    )

    assert routed.max_tokens == 64


def test_route_request_for_backend_preserves_lower_requested_max_tokens(monkeypatch):
    _patch_backend(monkeypatch, provider="mlx", cap=64)

    routed = upstreams.route_request_for_backend(
        _request(max_tokens=32),
        "local_mlx",
        "mlx-community/GLM-5.2-4bit",
    )

    assert routed.max_tokens == 32


def test_route_request_for_backend_defaults_missing_vllm_max_tokens_to_alias_cap(monkeypatch):
    aliases = {
        "default": _alias(
            backend="local_vllm",
            upstream_model="ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
            cap=2048,
        )
    }
    _patch_backend(monkeypatch, provider="vllm", aliases=aliases)

    routed = upstreams.route_request_for_backend(
        _request(model="default", max_tokens=None),
        "local_vllm",
        "ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
    )

    assert routed.max_tokens == 2048


def test_route_request_for_backend_uses_routed_alias_cap_for_auto_selector(monkeypatch):
    aliases = {
        "long": _alias(
            backend="local_mlx",
            upstream_model="mlx-community/GLM-5.2-4bit",
            cap=64,
        )
    }
    _patch_backend(monkeypatch, provider="mlx", aliases=aliases)

    routed = upstreams.route_request_for_backend(
        _request(model="auto", max_tokens=None),
        "local_mlx",
        "mlx-community/GLM-5.2-4bit",
    )

    assert routed.max_tokens == 64


def test_normalize_openai_tools_payload_strips_nonstandard_metadata():
    payload = {
        "tools": [
            {
                "type": "function",
                "ui_title": "Sample",
                "ui_group": "Built-In",
                "function": {
                    "name": "sample_tool",
                    "description": "A sample tool.",
                    "parameters": {
                        "type": "object",
                        "required": ["value"],
                        "properties": {"value": {"type": "string"}},
                    },
                    "extra_function_field": True,
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": "sample_tool", "extra": "drop"},
            "extra": "drop",
        },
    }

    normalized = upstreams._normalize_openai_tools_payload(payload)

    assert normalized["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "sample_tool",
                "description": "A sample tool.",
                "parameters": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            },
        }
    ]
    assert normalized["tool_choice"] == {"type": "function", "function": {"name": "sample_tool"}}
