from __future__ import annotations

from types import SimpleNamespace

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
    monkeypatch.setattr(router, "backend_provider_name", lambda backend: "vllm" if "vllm" in backend else "mlx")

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

    assert decision.backend == "local_vllm"
    assert decision.model == "coder-model"
    assert decision.reason == "policy:tools->coding->alias:coder"


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
    monkeypatch.setattr(router.S, "MLX_MODEL_STRONG", "served-mlx-model", raising=False)

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