from __future__ import annotations

import json

from app import model_integration_workspace as miw


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
    lifecycle = json.loads((tmp_path / "integration" / "lifecycle.backend.json").read_text(encoding="utf-8"))
    backend_entry = lifecycle[plan["backend_class"]]
    assert backend_entry["host"] == "ai1"
    assert backend_entry["estimated_vram_mb"] == 12000
    assert "Recommended lane" in backend_entry["notes"]

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "## Recommended Deployment Target" in readme
    assert "ai1" in readme
    assert (tmp_path / "services" / plan["service_name"] / "Dockerfile").exists()