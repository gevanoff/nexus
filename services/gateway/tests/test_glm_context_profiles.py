from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import coding_agent, context_budget, model_aliases, upstreams
from app.models import ChatCompletionRequest, ChatMessage


def _glm_alias(**overrides):
    values = {
        "backend": "local_mlx",
        "upstream_model": "mlx-community/GLM-5.2-4bit",
        "context_window": 131_072,
        "tools": True,
        "max_tokens_cap": 16_384,
        "max_input_tokens": 100_000,
        "coding_context_reset_tokens": 90_000,
        "thinking_enabled": True,
    }
    values.update(overrides)
    return model_aliases.ModelAlias(**values)


def test_alias_parser_loads_context_and_thinking_policy():
    alias = model_aliases._parse_alias_value({
        "backend": "local_mlx", "model": "mlx-community/GLM-5.2-4bit",
        "context_window": 262_144, "max_tokens_cap": 32_768,
        "max_input_tokens": 210_000, "coding_context_reset_tokens": 185_000,
        "thinking_enabled": True,
    })
    assert alias is not None
    assert alias.max_input_tokens == 210_000
    assert alias.coding_context_reset_tokens == 185_000
    assert alias.thinking_enabled is True


def test_repository_aliases_separate_glm_roles():
    aliases = json.loads(Path(model_aliases.__file__).with_name("model_aliases.json").read_text())["aliases"]
    assert aliases["glm-chat"]["context_window"] == 32_768
    assert aliases["glm-chat"]["thinking_enabled"] is False
    assert aliases["coder"]["coding_context_reset_tokens"] == 90_000
    assert aliases["coder"]["max_tokens_cap"] == 16_384
    assert aliases["reasoning"]["model"] == "mlx-community/GLM-5.2-4bit"
    assert aliases["reasoning"]["thinking_enabled"] is True
    assert aliases["long"]["context_window"] == 262_144
    assert aliases["long"]["max_input_tokens"] == 210_000
    assert aliases["long"]["coding_context_reset_tokens"] == 185_000
    assert aliases["long"]["max_tokens_cap"] == 32_768


def test_token_estimator_is_conservative():
    code = "def handler(value):\n    return value\n" * 100
    assert context_budget.estimate_text_tokens(code) >= len(code) // 3
    assert context_budget.estimate_text_tokens("漢字" * 100) >= 130


def test_alias_input_budget_overrides_legacy_char_guard(monkeypatch):
    alias = _glm_alias(max_input_tokens=1_000)
    monkeypatch.setattr(upstreams, "get_alias", lambda name: alias if name == "coder" else None)
    monkeypatch.setattr(upstreams, "_alias_matches_backend", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda _name: "mlx")
    monkeypatch.setattr(upstreams.S, "MLX_GLM_MAX_INPUT_CHARS", 10, raising=False)
    req = ChatCompletionRequest(model="coder", messages=[{"role": "user", "content": "small prompt"}])
    upstreams._enforce_mlx_glm_input_limit(req, backend_name="local_mlx", model_name=alias.upstream_model)


def test_alias_input_rejection_discloses_estimate(monkeypatch):
    alias = _glm_alias(max_input_tokens=20)
    monkeypatch.setattr(upstreams, "get_alias", lambda name: alias if name == "coder" else None)
    monkeypatch.setattr(upstreams, "_alias_matches_backend", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(upstreams, "backend_provider_name", lambda _name: "mlx")
    req = ChatCompletionRequest(model="coder", messages=[{"role": "user", "content": "x" * 300}])
    with pytest.raises(HTTPException) as caught:
        upstreams._enforce_mlx_glm_input_limit(req, backend_name="local_mlx", model_name=alias.upstream_model)
    assert caught.value.detail["input_tokens_estimate"] > 20
    assert caught.value.detail["token_estimator"] == context_budget.TOKEN_ESTIMATOR_NAME


def test_route_applies_coder_thinking_and_output_cap(monkeypatch):
    alias = _glm_alias()
    monkeypatch.setattr(upstreams, "get_alias", lambda name: alias if name == "coder" else None)
    monkeypatch.setattr(upstreams, "_alias_matches_backend", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(upstreams, "_resolve_backend_target", lambda _name: ("local_mlx", "mlx", "http://mlx.test/v1"))
    monkeypatch.setattr(upstreams, "_enforce_backend_input_limit", lambda *_args, **_kwargs: None)
    req = ChatCompletionRequest(model="coder", messages=[{"role": "user", "content": "debug"}], max_tokens=30_000)
    routed = upstreams.route_request_for_backend(req, "local_mlx", alias.upstream_model)
    assert routed.max_tokens == 16_384
    assert routed.chat_template_kwargs["enable_thinking"] is True


def test_route_disables_thinking_for_chat(monkeypatch):
    alias = _glm_alias(max_tokens_cap=4_096, max_input_tokens=26_000, coding_context_reset_tokens=None, thinking_enabled=False)
    monkeypatch.setattr(upstreams, "get_alias", lambda name: alias if name == "glm-chat" else None)
    monkeypatch.setattr(upstreams, "_alias_matches_backend", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(upstreams, "_resolve_backend_target", lambda _name: ("local_mlx", "mlx", "http://mlx.test/v1"))
    monkeypatch.setattr(upstreams, "_enforce_backend_input_limit", lambda *_args, **_kwargs: None)
    req = ChatCompletionRequest(model="glm-chat", messages=[{"role": "user", "content": "hello"}])
    routed = upstreams.route_request_for_backend(req, "local_mlx", alias.upstream_model)
    assert routed.max_tokens == 4_096
    assert routed.chat_template_kwargs["enable_thinking"] is False


def test_coding_profile_controls_compaction_and_output(monkeypatch):
    alias = _glm_alias()
    monkeypatch.setattr(coding_agent, "get_aliases", lambda: {"coder": alias})
    monkeypatch.setattr(coding_agent, "_backend_supports_tool_calling", lambda _backend: True)
    assert coding_agent._context_reset_tokens(64_000, model="coder") == 90_000
    assert coding_agent._max_completion_tokens_for_route("coder", "local_mlx") == 16_384
    assert coding_agent._messages_token_count([ChatMessage(role="user", content="x" * 3_000)]) >= 1_000


def test_unprofiled_route_keeps_legacy_fallback(monkeypatch):
    monkeypatch.setattr(coding_agent, "get_aliases", lambda: {})
    monkeypatch.setattr(coding_agent.S, "CODING_AGENT_CONTEXT_RESET_CHARS", 64_000, raising=False)
    assert coding_agent._context_reset_tokens(None, model="unconfigured") == 21_334
