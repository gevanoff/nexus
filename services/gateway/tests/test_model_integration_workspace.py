from __future__ import annotations

import json

from services.app import model_integration_workspace as miw


def test_build_integration_plan_routes_large_chat_model_to_ada2(monkeypatch):
    monkeypatch.setattr(
        miw,
        "fetch_model_metadata",
        lambda model_id: {
            "id": model_id,
            "library_name": "transformers",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation", "30B"],
            "config": {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]},
        },
    )

    plan = miw.build_integration_plan(model="Qwen/Qwen3-30B-A3B-Instruct")

    assert plan["runtime"] == "vllm"
    assert plan["deployment_target"]["host"] == "ada2"
    assert plan["deployment_target"]["backend_lane"] == "local_vllm"
    assert plan["deployment_target"]["estimated_vram_mb"] == 28000
    assert plan["estimated_model_size_b"] == 30.0


def test_build_integration_plan_prefers_mlx_host_native_lane(monkeypatch):
    monkeypatch.setattr(
        miw,
        "fetch_model_metadata",
        lambda model_id: {
            "id": model_id,
            "library_name": "mlx",
            "pipeline_tag": "text-generation",
            "tags": ["mlx", "mlx-community"],
            "config": {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]},
        },
    )

    plan = miw.build_integration_plan(model="mlx-community/Qwen3-8B-4bit")

    assert plan["runtime"] == "mlx"
    assert plan["containerize"] is False
    assert plan["deployment_target"]["host"] == "ai2"
    assert plan["deployment_target"]["deployment_mode"] == "host_native"


def test_scaffold_workspace_writes_topology_aware_files(monkeypatch, tmp_path):
    monkeypatch.setattr(
        miw,
        "fetch_model_metadata",
        lambda model_id: {
            "id": model_id,
            "library_name": "transformers",
            "pipeline_tag": "feature-extraction",
            "tags": ["embeddings", "feature-extraction"],
            "config": {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]},
        },
    )

    plan = miw.build_integration_plan(model="Qwen/Qwen3-Embedding-4B")
    created = miw.scaffold_workspace(tmp_path, plan)

    assert created
    assert plan["integration_strategy"] == "existing_vllm_model"
    assert plan["target_backend_class"] == "local_vllm_embeddings"
    assert not (tmp_path / "integration" / "lifecycle.backend.json").exists()
    assert not (tmp_path / "services" / plan["service_name"] / "Dockerfile").exists()

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Integration strategy: `existing_vllm_model`" in readme
    assert "not add a new backend service" in readme
    assert "ai1" in readme
    env_snippet = (tmp_path / "integration" / "vllm-model-env-snippet.env").read_text(encoding="utf-8")
    assert "VLLM_MODEL_EMBEDDINGS=Qwen/Qwen3-Embedding-4B" in env_snippet
    alias_snippet = json.loads((tmp_path / "integration" / "model-alias-snippet.json").read_text(encoding="utf-8"))
    aliases = alias_snippet["aliases"]
    assert list(aliases) == ["qwen3-embedding-4b"]
    assert aliases["qwen3-embedding-4b"]["backend"] == "local_vllm_embeddings"


def test_integration_host_lanes_fall_back_without_topology_files(monkeypatch):
    monkeypatch.setattr(miw, "_load_topology_manifest", lambda: {})
    monkeypatch.setattr(miw, "_load_backend_lifecycle", lambda: {})

    lanes = miw.integration_host_lanes()

    assert lanes
    assert any(lane["host"] == "ai2" and lane["runtime"] == "mlx" for lane in lanes)
    assert any(lane["host"] == "ada2" and lane["runtime"] == "vllm" for lane in lanes)
