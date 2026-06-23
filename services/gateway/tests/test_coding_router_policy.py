from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import router


def test_tool_bearing_coding_requests_prefer_coder_alias(monkeypatch):
    aliases = {
        "default": SimpleNamespace(backend="local_mlx", upstream_model="default-model", tools=True),
        "coder": SimpleNamespace(backend="local_vllm", upstream_model="coder-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: {})
    monkeypatch.setattr(router, "get_alias", lambda name: aliases.get(name))
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "_provider_default_backend", lambda provider: "local_mlx" if provider == "mlx" else provider)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: "active-mlx-huge-model")

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_mlx",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="auto",
        headers={"x-request-type": "coding"},
        messages=[{"role": "user", "content": "Fix the broken Edit button in tasks.js"}],
        has_tools=True,
        enable_policy=True,
        enable_request_type=True,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "active-mlx-huge-model"
    assert decision.reason == "policy:tools->coding->alias:coder"


def test_tool_request_skips_vllm_default_alias_without_native_tools(monkeypatch):
    aliases = {
        "default": SimpleNamespace(backend="local_vllm", upstream_model="default-vllm-model", tools=True),
        "coder": SimpleNamespace(backend="local_mlx", upstream_model="coder-mlx-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: {})
    monkeypatch.setattr(router, "get_alias", lambda name: aliases.get(name))
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router, "backend_supports_tool_calling", lambda backend: backend == "local_mlx")
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: None)

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="auto",
        headers={},
        messages=[{"role": "user", "content": "Use a tool to answer"}],
        has_tools=True,
        enable_policy=True,
        enable_request_type=True,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "coder-mlx-model"
    assert decision.reason == "policy:tools->alias:coder"


def test_tool_request_prefers_fast_alias_when_fast_native_tools_enabled(monkeypatch):
    aliases = {
        "default": SimpleNamespace(backend="local_vllm", upstream_model="default-vllm-model", tools=True),
        "fast": SimpleNamespace(backend="local_vllm_fast", upstream_model="fast-vllm-model", tools=True),
        "coder": SimpleNamespace(backend="local_mlx", upstream_model="coder-mlx-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: {})
    monkeypatch.setattr(router, "get_alias", lambda name: aliases.get(name))
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router, "backend_supports_tool_calling", lambda backend: backend in {"local_vllm_fast", "local_mlx"})
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: None)

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="auto",
        headers={},
        messages=[{"role": "user", "content": "Use a tool to answer"}],
        has_tools=True,
        enable_policy=True,
        enable_request_type=True,
    )

    assert decision.backend == "local_vllm_fast"
    assert decision.model == "fast-vllm-model"
    assert decision.reason == "policy:tools->alias:fast"


def test_direct_fast_alias_with_tools_uses_fast_when_native_tools_enabled(monkeypatch):
    aliases = {
        "fast": SimpleNamespace(backend="local_vllm_fast", upstream_model="fast-vllm-model", tools=True),
        "coder": SimpleNamespace(backend="local_mlx", upstream_model="coder-mlx-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router, "backend_supports_tool_calling", lambda backend: backend == "local_vllm_fast")
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: None)

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="fast",
        headers={},
        messages=[{"role": "user", "content": "Use a tool to answer"}],
        has_tools=True,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_vllm_fast"
    assert decision.model == "fast-vllm-model"
    assert decision.reason == "alias:model"


def test_direct_default_alias_with_tools_reroutes_when_vllm_tools_disabled(monkeypatch):
    aliases = {
        "default": SimpleNamespace(backend="local_vllm", upstream_model="default-vllm-model", tools=True),
        "coder": SimpleNamespace(backend="local_mlx", upstream_model="coder-mlx-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "get_alias", lambda name: aliases.get(name))
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router, "backend_supports_tool_calling", lambda backend: backend == "local_mlx")
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: None)

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="default",
        headers={},
        messages=[{"role": "user", "content": "Use a tool to answer"}],
        has_tools=True,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "coder-mlx-model"
    assert decision.reason == "policy:tools->alias:coder"


def test_direct_default_alias_with_tools_uses_vllm_when_native_tools_enabled(monkeypatch):
    aliases = {
        "default": SimpleNamespace(backend="local_vllm", upstream_model="default-vllm-model", tools=True),
        "coder": SimpleNamespace(backend="local_mlx", upstream_model="coder-mlx-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router, "backend_supports_tool_calling", lambda backend: True)
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: None)

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="default",
        headers={},
        messages=[{"role": "user", "content": "Use a tool to answer"}],
        has_tools=True,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_vllm"
    assert decision.model == "default-vllm-model"
    assert decision.reason == "alias:model"


def test_direct_coder_alias_tracks_active_mlx_huge_model(monkeypatch):
    aliases = {
        "coder": SimpleNamespace(backend="local_vllm", upstream_model="stale-coder-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "_provider_default_backend", lambda provider: "local_mlx" if provider == "mlx" else provider)
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: "active-mlx-huge-model")

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="coder",
        headers={},
        messages=[{"role": "user", "content": "hi"}],
        has_tools=False,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "active-mlx-huge-model"
    assert decision.reason == "alias:model"


def test_policy_coding_without_tools_uses_active_mlx_coder(monkeypatch):
    aliases = {
        "coder": SimpleNamespace(backend="local_vllm", upstream_model="stale-coder-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "get_alias", lambda name: aliases.get(name))
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "_provider_default_backend", lambda provider: "local_mlx" if provider == "mlx" else provider)
    monkeypatch.setattr(router.coding_model_policy, "current_coder_model", lambda: "active-mlx-huge-model")

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="auto",
        headers={"x-request-type": "coding"},
        messages=[{"role": "user", "content": "Fix services/gateway/app/router.py"}],
        has_tools=False,
        enable_policy=True,
        enable_request_type=True,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "active-mlx-huge-model"
    assert decision.reason == "policy:coding->alias:coder"


def test_vllm_fast_selector_normalizes_to_fast_model(monkeypatch):
    monkeypatch.setattr(router, "get_aliases", lambda: {})
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: "local_vllm_fast" if name in {"vllm_fast", "local_vllm_fast"} else name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: "local_vllm_fast" if name == "vllm_fast" else None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")
    monkeypatch.setattr(router.S, "VLLM_MODEL_FAST", "served-fast-model", raising=False)

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_mlx",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="vllm_fast",
        headers={},
        messages=[{"role": "user", "content": "hi"}],
        has_tools=False,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_vllm_fast"
    assert decision.model == "served-fast-model"
    assert decision.reason == "selector:model"


def test_mlx_selector_prefers_runtime_selector_over_alias(monkeypatch):
    aliases = {
        "mlx": SimpleNamespace(backend="local_mlx", upstream_model="stale-alias-model", tools=True),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: "local_mlx" if name in {"mlx", "local_mlx"} else name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: "local_mlx" if name == "mlx" else None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "mlx" if "mlx" in backend else "vllm")
    monkeypatch.setattr(router.mlx_huge_lane, "route_model", lambda: "served-mlx-model")

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_mlx",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="mlx",
        headers={},
        messages=[{"role": "user", "content": "hi"}],
        has_tools=False,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "served-mlx-model"
    assert decision.reason == "selector:model"


def test_mlx_huge_lane_overrides_long_alias(monkeypatch):
    aliases = {
        "long": SimpleNamespace(
            backend="local_mlx",
            upstream_model="mlx-community/MiniMax-M2.5-8bit",
            tools=False,
            context_window=65536,
        ),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: "local_mlx" if name in {"mlx", "local_mlx"} else name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "mlx" if "mlx" in backend else "vllm")
    monkeypatch.setattr(router.mlx_huge_lane, "is_huge_model", lambda model: model.endswith("MiniMax-M2.5-8bit"))
    monkeypatch.setattr(router.mlx_huge_lane, "route_model", lambda: "mlx-community/DeepSeek-R1-0528-4bit")

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_mlx",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="long",
        headers={},
        messages=[{"role": "user", "content": "hi"}],
        has_tools=False,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "mlx-community/DeepSeek-R1-0528-4bit"
    assert decision.reason == "alias:model"


def test_explicit_fast_alias_large_context_falls_back_to_long(monkeypatch):
    aliases = {
        "fast": SimpleNamespace(
            backend="local_vllm_fast",
            upstream_model="fast-vllm-model",
            tools=False,
            context_window=2048,
        ),
        "long": SimpleNamespace(
            backend="local_mlx",
            upstream_model="long-mlx-model",
            tools=False,
            context_window=65536,
        ),
    }

    monkeypatch.setattr(router, "get_aliases", lambda: aliases)
    monkeypatch.setattr(router, "_resolved_backend_name", lambda name: name)
    monkeypatch.setattr(router, "_known_backend_name", lambda name: None)
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")

    decision = router.decide_route(
        cfg=router.RouterConfig(
            default_backend="local_vllm",
            primary_strong_model="strong-model",
            primary_fast_model="fast-model",
        ),
        request_model="fast",
        headers={},
        messages=[{"role": "user", "content": "x" * 9000}],
        has_tools=False,
        enable_policy=False,
        enable_request_type=False,
    )

    assert decision.backend == "local_mlx"
    assert decision.model == "long-mlx-model"
    assert decision.reason == "policy:alias_context->fast->alias:long"
