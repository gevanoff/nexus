import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import openai_routes
from app.models import ChatCompletionRequest, ChatMessage


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="coder",
        messages=[ChatMessage(role="user", content="hello")],
    )


def test_huge_request_is_rejected_during_manual_transition(monkeypatch) -> None:
    route = SimpleNamespace(backend="local_mlx", model="mlx-community/GLM-5.2-DQ4plus-q8", reason="alias:coder")
    monkeypatch.setattr(openai_routes, "decide_route", lambda **_kwargs: route)
    monkeypatch.setattr(
        openai_routes,
        "get_registry",
        lambda: SimpleNamespace(resolve_backend_class=lambda _backend: "local_mlx"),
    )
    monkeypatch.setattr(
        openai_routes.mlx_huge_lane,
        "request_block",
        lambda _model: {
            "error": "mlx_huge_transition_in_progress",
            "message": "switching",
            "retryable": True,
        },
    )

    with pytest.raises(HTTPException) as exc:
        openai_routes._route_chat_request(_request(), headers={})

    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "mlx_huge_transition_in_progress"


def test_nonresident_huge_request_requires_admin_switch(monkeypatch) -> None:
    route = SimpleNamespace(backend="local_mlx", model="mlx-community/DeepSeek-R1-0528-4bit", reason="alias:deepseek-r1")
    monkeypatch.setattr(openai_routes, "decide_route", lambda **_kwargs: route)
    monkeypatch.setattr(
        openai_routes,
        "get_registry",
        lambda: SimpleNamespace(resolve_backend_class=lambda _backend: "local_mlx"),
    )
    monkeypatch.setattr(
        openai_routes.mlx_huge_lane,
        "request_block",
        lambda _model: {
            "error": "mlx_huge_model_not_resident",
            "message": "manual switch required",
            "retryable": False,
        },
    )

    with pytest.raises(HTTPException) as exc:
        openai_routes._route_chat_request(_request(), headers={})

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "mlx_huge_model_not_resident"
