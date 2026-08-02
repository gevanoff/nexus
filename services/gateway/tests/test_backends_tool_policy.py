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
            base_url="http://stackrot:8001/v1",
            backend_class="local_vllm_fast",
            hostname="stackrot",
            source="etcd",
        )
    }

    backends._apply_service_records(registry, records)

    assert registry.get_backend("local_vllm_fast").base_url == "http://host.docker.internal:18001/v1"
    assert registry.service_records["vllm-fast"].base_url == "http://host.docker.internal:18001/v1"
    assert registry.service_records["vllm-fast"].metadata_url == "http://host.docker.internal:18001/v1/metadata"
    assert registry.service_records["vllm-fast"].hostname == "stackrot"


def test_production_topology_configures_validated_vllm_tool_profiles():
    repo_root = Path(__file__).resolve().parents[3]
    topology = json.loads((repo_root / "deploy" / "topology" / "production.json").read_text(encoding="utf-8"))
    env = topology["defaults"]["env"]

    assert env["VLLM_TOOL_PROFILE"] == "xlam_mistral_parallel"
    assert env["VLLM_FAST_TOOL_PROFILE"] == "mistral_serial"
    assert env["VLLM_NATIVE_TOOLS_ENABLED"] == "true"
    assert env["VLLM_FAST_NATIVE_TOOLS_ENABLED"] == "true"
    assert env["VLLM_ENABLE_AUTO_TOOL_CHOICE"] == "true"
    assert env["VLLM_FAST_ENABLE_AUTO_TOOL_CHOICE"] == "true"
    assert env["VLLM_TOOL_CALL_PARSER"] == "xlam"
    assert env["VLLM_FAST_TOOL_CALL_PARSER"] == "mistral"
    assert env["VLLM_CHAT_TEMPLATE"] == "/vllm-workspace/examples/tool_chat_template_mistral_parallel.jinja"
    assert env["VLLM_FAST_CHAT_TEMPLATE"] == ""
    auto_qualification_models = env["MODEL_TOOL_QUALIFICATION_AUTO_RUN_MODELS"].split(",")
    assert auto_qualification_models[0] == "mlx"
    assert auto_qualification_models[-1] == "fast"
    assert env["TELEGRAM_REQUIRE_MENTION"] == "true"
    assert env["TELEGRAM_MENTION_PATTERNS"] == "Nexus"
    assert env["NEXUS_TOOL_EXECUTION_DEFAULT"] == "client_exec"
    assert env["NEXUS_AUTO_INJECT_TOOLS"] == "true"
    assert env["NEXUS_AUTO_INJECT_TOOLSETS"] == "core,repo,ops"
    assert env["NEXUS_UI_GATEWAY_EXEC"] == "false"
    assert "vllm-fast" in topology["hosts"]["stackrot"]["components"]
    assert topology["hosts"]["stackrot"]["env"]["VLLM_FAST_TOKENIZER"] == "cyankiwi/Devstral-Small-2507-AWQ-4bit"
    assert topology["hosts"]["stackrot"]["env"]["VLLM_FAST_TOKENIZER_MODE"] == "mistral"
    assert env["VLLM_FAST_MAX_MODEL_LEN"] == "65536"
    assert env["VLLM_FAST_KV_CACHE_DTYPE"] == "fp8_e5m2"
    assert env["VLLM_FAST_CALCULATE_KV_SCALES"] == "true"
    assert env["VLLM_FAST_MAX_NUM_SEQS"] == "1"
    assert env["VLLM_FAST_MAX_NUM_BATCHED_TOKENS"] == "8192"
    assert topology["hosts"]["stackrot"]["env"]["VLLM_FAST_GPU_MEMORY_UTILIZATION"] == "0.86"
    assert topology["hosts"]["stackrot"]["env"]["VLLM_FAST_MAX_MODEL_LEN"] == "65536"
    assert topology["hosts"]["stackrot"]["env"]["VLLM_FAST_CPU_OFFLOAD_GB"] == "0"
    compose = (repo_root / "docker-compose.vllm-fast.yml").read_text(encoding="utf-8")
    launcher = (repo_root / "deploy" / "scripts" / "run-vllm-openai.sh").read_text(encoding="utf-8")
    assert "NEXUS_VLLM_CALCULATE_KV_SCALES=${VLLM_FAST_CALCULATE_KV_SCALES:-false}" in compose
    assert "NEXUS_VLLM_MAX_NUM_SEQS=${VLLM_FAST_MAX_NUM_SEQS:-}" in compose
    assert "NEXUS_VLLM_MAX_NUM_BATCHED_TOKENS=${VLLM_FAST_MAX_NUM_BATCHED_TOKENS:-}" in compose
    assert '--calculate-kv-scales' in launcher
    assert '--max-num-seqs "$NEXUS_VLLM_MAX_NUM_SEQS"' in launcher
    assert '--max-num-batched-tokens "$NEXUS_VLLM_MAX_NUM_BATCHED_TOKENS"' in launcher
    assert "vllm-strong" in topology["hosts"]["ada2"]["components"]
    assert env["VLLM_MAX_MODEL_LEN"] == "65536"
    assert topology["hosts"]["ada2"]["env"]["VLLM_MAX_MODEL_LEN"] == "65536"
    assert topology["hosts"]["meltdown"]["env"]["VLLM_EMBEDDINGS_BACKEND_CLASS"] == "local_vllm_embeddings"
    assert topology["hosts"]["meltdown"]["env"]["VLLM_MELTDOWN_BACKEND_CLASS"] == "local_vllm_meltdown"
    ai2_overrides = set(topology["hosts"]["ai2"]["env"]["BACKEND_ENV_BASE_URL_OVERRIDES"].split(","))
    assert {
        "local_vllm",
        "local_vllm_fast",
        "local_vllm_embeddings",
        "local_vllm_meltdown",
        "gpu_heavy",
        "gpu_fast",
        "lighton_ocr",
        "personaplex",
        "ltx_video",
    } <= ai2_overrides
