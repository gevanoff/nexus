import os
from types import SimpleNamespace

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import openai_routes
from app.model_aliases import ModelAlias, _parse_alias_value
from app.models import ChatCompletionRequest, ChatMessage


def _request(model: str, **updates) -> ChatCompletionRequest:
    values = {
        "model": model,
        "messages": [ChatMessage(role="user", content="search the web")],
    }
    values.update(updates)
    return ChatCompletionRequest(**values)


def _install_routes(monkeypatch):
    aliases = {
        "fast": ModelAlias("local_vllm_fast", "fast-model", tools=True, tool_fallback_alias="long"),
        "stackrot-chat": ModelAlias(
            "local_vllm_fast",
            "fast-model",
            tools=True,
            tool_mode="gateway_exec",
            tool_mode_explicit=True,
            tool_fallback_alias="long",
            soul="stackrot",
        ),
        "long": ModelAlias("local_mlx", "long-model", tools=True),
    }
    calls = []

    def fake_decide_route(**kwargs):
        model = kwargs["request_model"]
        calls.append(model)
        alias = aliases[model]
        return SimpleNamespace(backend=alias.backend, model=alias.upstream_model, reason=f"alias:{model}")

    monkeypatch.setattr(openai_routes, "get_aliases", lambda: aliases)
    monkeypatch.setattr(openai_routes, "decide_route", fake_decide_route)
    monkeypatch.setattr(
        openai_routes,
        "get_registry",
        lambda: SimpleNamespace(resolve_backend_class=lambda backend: backend),
    )
    monkeypatch.setattr(openai_routes.mlx_huge_lane, "request_block", lambda _model: None)
    return calls


def test_alias_parser_normalizes_tool_fallback_alias():
    alias = _parse_alias_value(
        {
            "backend": "local_vllm_fast",
            "model": "fast-model",
            "tools": True,
            "tool_mode": "gateway_exec",
            "auto_inject_tools": True,
            "toolsets": ["core", "web"],
            "max_tool_rounds": 2,
            "tool_fallback_alias": " LONG ",
        }
    )

    assert alias is not None
    assert alias.tool_mode == "gateway_exec"
    assert alias.tool_mode_explicit is True
    assert alias.auto_inject_tools is True
    assert alias.toolsets == ("core", "web")
    assert alias.max_tool_rounds == 2
    assert alias.tool_fallback_alias == "long"


def test_fast_chat_without_tools_stays_on_fast_backend(monkeypatch):
    calls = _install_routes(monkeypatch)

    route, backend, alias_name = openai_routes._route_chat_request(_request("fast"), headers={})

    assert calls == ["fast"]
    assert (route.model, backend, alias_name) == ("fast-model", "local_vllm_fast", "fast")


def test_fast_chat_with_client_tools_uses_configured_fallback(monkeypatch):
    calls = _install_routes(monkeypatch)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    route, backend, alias_name = openai_routes._route_chat_request(
        _request("fast", tools=tools),
        headers={},
    )

    assert calls == ["fast", "long"]
    assert (route.model, backend, alias_name) == ("long-model", "local_mlx", "fast")


def test_alias_gateway_exec_uses_fallback_and_preserves_persona_alias(monkeypatch):
    calls = _install_routes(monkeypatch)

    route, backend, alias_name = openai_routes._route_chat_request(
        _request("stackrot-chat"),
        headers={},
    )

    assert calls == ["stackrot-chat", "long"]
    assert (route.model, backend, alias_name) == ("long-model", "local_mlx", "stackrot-chat")
