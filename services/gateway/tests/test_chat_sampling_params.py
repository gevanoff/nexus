from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import upstreams
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


def test_vllm_qwen3_defaults_disable_thinking_without_overriding_user_values(monkeypatch):
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda backend_name: "vllm")

    payload = upstreams._apply_backend_generation_defaults(
        {"model": "Qwen3-test", "messages": [], "chat_template_kwargs": {"enable_thinking": True}, "repetition_penalty": 1.2},
        backend_name="local_vllm_fast",
        model_name="unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M",
    )
    assert payload["chat_template_kwargs"]["enable_thinking"] is True
    assert payload["repetition_penalty"] == 1.2

    payload = upstreams._apply_backend_generation_defaults(
        {"model": "Qwen3-test", "messages": []},
        backend_name="local_vllm_fast",
        model_name="unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M",
    )
    assert payload["chat_template_kwargs"]["enable_thinking"] is False
    assert payload["repetition_penalty"] == 1.12


def test_vllm_magistral_defaults_use_recommended_sampling_without_overriding_user_values(monkeypatch):
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda backend_name: "vllm")

    payload = upstreams._apply_backend_generation_defaults(
        {"model": "Magistral-test", "messages": []},
        backend_name="local_vllm",
        model_name="ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
    )
    assert payload["temperature"] == 0.7
    assert payload["top_p"] == 0.95

    payload = upstreams._apply_backend_generation_defaults(
        {"model": "Magistral-test", "messages": [], "temperature": 0.2, "top_p": 0.8},
        backend_name="local_vllm",
        model_name="ConicCat/Magistral-Small-2509-Text-Only-FP8-Dynamic",
    )
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.8


def test_vllm_devstral_defaults_use_recommended_temperature_without_overriding_user_value(monkeypatch):
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda backend_name: "vllm")

    payload = upstreams._apply_backend_generation_defaults(
        {"model": "Devstral-test", "messages": []},
        backend_name="local_vllm_fast",
        model_name="cyankiwi/Devstral-Small-2507-AWQ-4bit",
    )
    assert payload["temperature"] == 0.15

    payload = upstreams._apply_backend_generation_defaults(
        {"model": "Devstral-test", "messages": [], "temperature": 0.4},
        backend_name="local_vllm_fast",
        model_name="cyankiwi/Devstral-Small-2507-AWQ-4bit",
    )
    assert payload["temperature"] == 0.4
