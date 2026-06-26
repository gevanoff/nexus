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


def _request(model: str = "long", max_tokens: int | None = None) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="Reply OK only.")],
        max_tokens=max_tokens,
        stream=True,
    )


def _patch_backend(monkeypatch, *, cap: int = 64) -> None:
    monkeypatch.setattr(upstreams, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda backend_name: "mlx")
    monkeypatch.setattr(upstreams, "_enforce_mlx_glm_input_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        upstreams,
        "get_alias",
        lambda name: SimpleNamespace(backend="local_mlx", upstream_model="mlx-community/GLM-5.2-DQ4plus-q8", max_tokens_cap=cap)
        if name == "long"
        else None,
    )


def test_route_request_for_backend_defaults_missing_max_tokens_to_alias_cap(monkeypatch):
    _patch_backend(monkeypatch, cap=64)

    routed = upstreams.route_request_for_backend(
        _request(max_tokens=None),
        "local_mlx",
        "mlx-community/GLM-5.2-DQ4plus-q8",
    )

    assert routed.model == "mlx-community/GLM-5.2-DQ4plus-q8"
    assert routed.max_tokens == 64


def test_route_request_for_backend_caps_oversized_max_tokens(monkeypatch):
    _patch_backend(monkeypatch, cap=64)

    routed = upstreams.route_request_for_backend(
        _request(max_tokens=1024),
        "local_mlx",
        "mlx-community/GLM-5.2-DQ4plus-q8",
    )

    assert routed.max_tokens == 64


def test_route_request_for_backend_preserves_lower_requested_max_tokens(monkeypatch):
    _patch_backend(monkeypatch, cap=64)

    routed = upstreams.route_request_for_backend(
        _request(max_tokens=32),
        "local_mlx",
        "mlx-community/GLM-5.2-DQ4plus-q8",
    )

    assert routed.max_tokens == 32
