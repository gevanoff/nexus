from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from ..app import upstreams
from app.models import ChatCompletionRequest, ChatMessage


def test_route_request_for_backend_preserves_local_sampling_params(monkeypatch):
    monkeypatch.setattr(upstreams, "_resolve_backend_target", lambda backend_name: (backend_name, "mlx", "http://example.invalid/v1"))
    req = ChatCompletionRequest(
        model="coder",
        messages=[ChatMessage(role="user", content="fix it")],
        temperature=0.2,
        top_p=0.85,
        top_k=40,
        min_p=0.03,
        repetition_penalty=1.03,
        frequency_penalty=0.1,
        presence_penalty=0.2,
        stop=["</patch>"],
        seed=1234,
        max_tokens=8192,
        stream=True,
    )

    routed = upstreams.route_request_for_backend(req, "local_mlx", "actual-model")
    payload = routed.model_dump(exclude_none=True)

    assert payload["model"] == "actual-model"
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.85
    assert payload["top_k"] == 40
    assert payload["min_p"] == 0.03
    assert payload["repetition_penalty"] == 1.03
    assert payload["frequency_penalty"] == 0.1
    assert payload["presence_penalty"] == 0.2
    assert payload["stop"] == ["</patch>"]
    assert payload["seed"] == 1234
    assert payload["max_tokens"] == 8192
    assert payload["stream"] is True
