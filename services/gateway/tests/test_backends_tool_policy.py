from __future__ import annotations

import json
import os
from pathlib import Path

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


def test_env_base_url_override_keeps_static_proxy_url_for_etcd_record(monkeypatch):
    monkeypatch.setattr(backends.S, "VLLM_FAST_BASE_URL", "http://host.docker.internal:18001/v1", raising=False)
    monkeypatch.setattr(backends.S, "BACKEND_ENV_BASE_URL_OVERRIDES", "local_vllm_fast", raising=False)

    registry = backends._default_registry()
    records = {
        "vllm-fast": backends.ServiceRecord(
            name="vllm-fast",
            base_url="http://ai1:8001/v1",
            backend_class="local_vllm_fast",
            hostname="ai1",
            source="etcd",
        )
    }

    backends._apply_service_records(registry, records)

    assert registry.get_backend("local_vllm_fast").base_url == "http://host.docker.internal:18001/v1"
    assert registry.service_records["vllm-fast"].base_url == "http://host.docker.internal:18001/v1"
    assert registry.service_records["vllm-fast"].metadata_url == "http://host.docker.internal:18001/v1/metadata"
    assert registry.service_records["vllm-fast"].hostname == "ai1"


def test_production_topology_disables_unvalidated_vllm_auto_tool_flags():
    repo_root = Path(__file__).resolve().parents[3]
    topology = json.loads((repo_root / "deploy" / "topology" / "production.json").read_text(encoding="utf-8"))
    env = topology["defaults"]["env"]

    assert env["VLLM_NATIVE_TOOLS_ENABLED"] == "false"
    assert env["VLLM_FAST_NATIVE_TOOLS_ENABLED"] == "false"
    assert env["VLLM_ENABLE_AUTO_TOOL_CHOICE"] == "false"
    assert env["VLLM_FAST_ENABLE_AUTO_TOOL_CHOICE"] == "false"
    assert env["VLLM_TOOL_CALL_PARSER"] == ""
    assert env["VLLM_FAST_TOOL_CALL_PARSER"] == ""
    assert "vllm-fast" in topology["hosts"]["ai1"]["components"]
    assert topology["hosts"]["ai1"]["env"]["VLLM_FAST_TOKENIZER"] == "cyankiwi/Devstral-Small-2507-AWQ-4bit"
    assert topology["hosts"]["ai1"]["env"]["VLLM_FAST_TOKENIZER_MODE"] == "mistral"
    assert "vllm-strong" in topology["hosts"]["ada2"]["components"]
    assert topology["hosts"]["meltdown"]["env"]["VLLM_EMBEDDINGS_BACKEND_CLASS"] == "local_vllm_embeddings"
    ai2_overrides = set(topology["hosts"]["ai2"]["env"]["BACKEND_ENV_BASE_URL_OVERRIDES"].split(","))
    assert {
        "local_vllm",
        "local_vllm_fast",
        "local_vllm_embeddings",
        "gpu_heavy",
        "gpu_fast",
        "lighton_ocr",
        "personaplex",
        "skyreels_v2",
    } <= ai2_overrides
