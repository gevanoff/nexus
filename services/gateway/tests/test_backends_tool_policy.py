from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import backends


def test_vllm_native_tool_flags_override_backend_payload_policy(monkeypatch, tmp_path):
    cfg = tmp_path / "backends.yaml"
    cfg.write_text(
        """
backends:
  local_vllm:
    class: local_vllm
    provider: vllm
    base_url: http://vllm:8000/v1
    description: strong
    supported_capabilities: [chat]
    concurrency_limits: {chat: 1}
    health: {liveness: /models, readiness: /models}
    payload_policy:
      supports_tool_calling: false
  local_vllm_fast:
    class: local_vllm_fast
    provider: vllm
    base_url: http://vllm-fast:8000/v1
    description: fast
    supported_capabilities: [chat]
    concurrency_limits: {chat: 1}
    health: {liveness: /models, readiness: /models}
    payload_policy:
      supports_tool_calling: false
legacy_mapping: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(backends.S, "VLLM_NATIVE_TOOLS_ENABLED", True, raising=False)
    monkeypatch.setattr(backends.S, "VLLM_FAST_NATIVE_TOOLS_ENABLED", False, raising=False)

    registry = backends.load_backends_config(cfg)

    assert registry.get_backend("local_vllm").payload_policy["supports_tool_calling"] is True
    assert registry.get_backend("local_vllm_fast").payload_policy["supports_tool_calling"] is False


def test_vllm_native_tool_flags_default_disabled_in_minimal_registry(monkeypatch):
    monkeypatch.setattr(backends.S, "VLLM_NATIVE_TOOLS_ENABLED", False, raising=False)
    monkeypatch.setattr(backends.S, "VLLM_FAST_NATIVE_TOOLS_ENABLED", False, raising=False)

    registry = backends._default_registry()

    assert registry.get_backend("local_vllm").payload_policy["supports_tool_calling"] is False
    assert registry.get_backend("local_vllm_fast").payload_policy["supports_tool_calling"] is False
