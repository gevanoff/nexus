from __future__ import annotations

import json

from app import model_integration_workspace as miw


def _metadata(*, pipeline: str, library: str = "transformers", tags=None, model_type: str = "", architecture: str = ""):
    return {
        "id": "fixture/model",
        "pipeline_tag": pipeline,
        "library_name": library,
        "tags": list(tags or []),
        "config": {"model_type": model_type, "architectures": [architecture] if architecture else []},
        "siblings": [{"rfilename": "model.safetensors", "size": 2 * 1024**3}],
    }


def test_model_metadata_route_classification():
    cases = [
        ("feature-extraction", "transformers", ["bge"], "embeddings"),
        ("text-to-image", "diffusers", ["sdxl"], "images"),
        ("text-to-speech", "transformers", ["speech-synthesis"], "tts"),
        ("text-to-video", "diffusers", ["skyreels"], "video"),
        ("document-question-answering", "transformers", ["ocr"], "ocr"),
    ]
    for pipeline, library, tags, expected in cases:
        result = miw.classify_model("owner/model", _metadata(pipeline=pipeline, library=library, tags=tags))
        assert result["route_kind"] == expected
        assert result["confidence"] in {"high", "medium"}
        assert result["reasons"]


def test_model_runtime_classification_is_conservative():
    mlx = miw.classify_model("mlx-community/GLM-5.2-4bit", _metadata(pipeline="text-generation", library="mlx", model_type="glm", architecture="GlmForCausalLM"))
    image = miw.classify_model("black-forest-labs/FLUX.1", _metadata(pipeline="text-to-image", library="diffusers", tags=["flux"]))
    weird = miw.classify_model("owner/weird", _metadata(pipeline="", library="custom"))
    assert mlx["runtime"] == "mlx"
    assert image["runtime"] == "diffusers"
    assert weird == {**weird, "route_kind": "json", "runtime": "custom", "confidence": "low"}


def test_model_host_placement_and_dossier_schema(monkeypatch):
    monkeypatch.setattr(miw, "fetch_model_metadata", lambda _model: _metadata(pipeline="text-generation", model_type="qwen3", architecture="Qwen3ForCausalLM", tags=["30B", "4bit"]))
    plan = miw.build_integration_plan(model="owner/Model-30B-4bit")
    dossier = plan["dossier"]
    assert plan["deployment_target"]["host"] == "ada2"
    assert dossier["schema"] == "nexus_model_integration.v1"
    assert dossier["classification"]["reasons"]
    assert dossier["placement"]["recommended_backend_lane"] == "local_vllm"
    assert dossier["activation"]["can_be_activated_from_resources_ui"] is True
    assert dossier["ui_integration"]["chat_ui"] is True


def test_model_integration_existing_vllm_lane_generates_catalogs(monkeypatch, tmp_path):
    monkeypatch.setattr(miw, "fetch_model_metadata", lambda _model: _metadata(pipeline="text-generation", model_type="qwen3", architecture="Qwen3ForCausalLM", tags=["8B"]))
    plan = miw.build_integration_plan(model="owner/Chat-8B")
    miw.scaffold_workspace(tmp_path, plan)
    assert plan["integration_strategy"] == "existing_vllm_model"
    assert not (tmp_path / "services" / plan["service_name"]).exists()
    assert json.loads((tmp_path / "integration" / "model-integration-dossier.json").read_text())["schema"] == "nexus_model_integration.v1"
    assert json.loads((tmp_path / "integration" / "resource-activation-entry.json").read_text())["activation"]["enabled"] is False
    assert json.loads((tmp_path / "integration" / "ui-catalog-entry.json").read_text())["route_kind"] == "chat"
    assert json.loads((tmp_path / "deploy" / "topology" / "model_integrations.json").read_text())["integrations"]


def test_model_integration_host_native_mlx_and_prompt_sequence(monkeypatch, tmp_path):
    monkeypatch.setattr(miw, "fetch_model_metadata", lambda _model: _metadata(pipeline="text-generation", library="mlx", model_type="glm", architecture="GlmForCausalLM", tags=["mlx", "4bit"]))
    plan = miw.build_integration_plan(model="mlx-community/GLM-5.2-4bit")
    miw.scaffold_workspace(tmp_path, plan)
    assert plan["runtime"] == "mlx"
    assert plan["deployment_target"]["host"] == "ai2"
    assert (tmp_path / "host_native" / plan["service_name"] / "model.env.example").exists()
    assert "1. Parse the requested model reference." in plan["prompt"]
    assert "Do not repeatedly re-read unchanged metadata" in plan["prompt"]


def test_model_integration_transformers_and_image_shims(monkeypatch, tmp_path):
    monkeypatch.setattr(miw, "fetch_model_metadata", lambda _model: _metadata(pipeline="text-to-image", library="diffusers", tags=["sdxl"]))
    plan = miw.build_integration_plan(model="owner/SDXL-Fixture")
    miw.scaffold_workspace(tmp_path, plan)
    service = tmp_path / "services" / plan["service_name"]
    assert plan["runtime"] == "diffusers"
    assert plan["route_kind"] == "images"
    assert (service / "Dockerfile").exists()
    assert (service / "app" / "main.py").exists()
    catalog = json.loads((tmp_path / "integration" / "ui-catalog-entry.json").read_text())
    assert catalog["ui"]["image_ui"] is True
