from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException


IMAGES_APP = Path(__file__).resolve().parents[2] / "images" / "app"


def _load_images_modules(*names: str):
    saved = {key: value for key, value in sys.modules.items() if key == "app" or key.startswith("app.")}
    for key in list(saved):
        sys.modules.pop(key, None)
    package = ModuleType("app")
    package.__path__ = [str(IMAGES_APP)]
    sys.modules["app"] = package
    try:
        loaded = [importlib.import_module(f"app.{name}") for name in names]
    finally:
        for key in [key for key in list(sys.modules) if key == "app" or key.startswith("app.")]:
            sys.modules.pop(key, None)
        sys.modules.update(saved)
    return loaded


workflow_routing, = _load_images_modules("workflow_routing")


def _write_graph(tmp_path: Path, name: str, node_type: str) -> str:
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "loader",
                        "data": {
                            "type": node_type,
                            "inputs": {
                                "model": {"value": None},
                            },
                        },
                    }
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _cfg(default_graph: str, *, default_model: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        graph_template_path=default_graph,
        output_node_id="default-output",
        default_model=default_model or None,
        model_presets_json=None,
        model_input_mode="id",
    )


class FakeShim:
    candidates: list[dict] = []

    @classmethod
    def _list_invokeai_models(cls, *, cfg):
        return "fake://models", list(cls.candidates)

    @staticmethod
    def _candidate_strings(candidate):
        return [
            str(candidate.get(key) or "").strip()
            for key in ("key", "id", "name", "model_name", "hash")
            if str(candidate.get(key) or "").strip()
        ]

    @staticmethod
    def _normalize_invokeai_candidate(candidate):
        return dict(candidate)

    @staticmethod
    def _parse_model_presets(raw):
        return json.loads(raw) if raw else {}


def test_configured_workflows_adds_default_family_and_explicit_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdxl = _write_graph(tmp_path, "sdxl.json", "sdxl_model_loader")
    flux = _write_graph(tmp_path, "flux.json", "flux_model_loader")
    monkeypatch.setenv("SHIM_GENERATION_WORKFLOWS_JSON", json.dumps({"flux": flux}))

    specs = workflow_routing.configured_workflows(_cfg(sdxl))

    assert specs["sdxl"].path == sdxl
    assert specs["sdxl"].source == "SHIM_GRAPH_TEMPLATE_PATH"
    assert specs["flux"].path == flux


def test_select_workflow_matches_model_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdxl = _write_graph(tmp_path, "sdxl.json", "sdxl_model_loader")
    flux = _write_graph(tmp_path, "flux.json", "flux_model_loader")
    monkeypatch.setenv("SHIM_GENERATION_WORKFLOWS_JSON", json.dumps({"flux": {"path": flux}}))

    spec = workflow_routing.select_workflow(
        {"key": "flux-id", "name": "FLUX.1 Dev", "base": "flux", "type": "main"},
        _cfg(sdxl),
    )

    assert spec.family == "flux"
    assert spec.path == flux
    assert spec.output_node_id is None


def test_default_model_prefers_family_with_configured_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdxl = _write_graph(tmp_path, "sdxl.json", "sdxl_model_loader")
    monkeypatch.delenv("SHIM_GENERATION_WORKFLOWS_JSON", raising=False)
    FakeShim.candidates = [
        {"key": "flux-id", "name": "FLUX.1 Dev", "base": "flux", "type": "main"},
        {
            "key": "juggernaut-id",
            "name": "Juggernaut XL v9",
            "base": "sdxl",
            "type": "main",
        },
    ]

    resolved = workflow_routing.resolve_requested_model(
        FakeShim,
        SimpleNamespace(model=None),
        _cfg(sdxl),
    )

    assert resolved["key"] == "juggernaut-id"


def test_missing_family_workflow_error_names_model_id_and_available_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdxl = _write_graph(tmp_path, "sdxl.json", "sdxl_model_loader")
    monkeypatch.delenv("SHIM_GENERATION_WORKFLOWS_JSON", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        workflow_routing.select_workflow(
            {"key": "flux-uuid", "name": "FLUX.1 Dev", "base": "flux", "type": "main"},
            _cfg(sdxl),
        )

    assert exc_info.value.status_code == 400
    detail = str(exc_info.value.detail)
    assert "FLUX.1 Dev (id flux-uuid)" in detail
    assert "Detected model family: flux" in detail
    assert "Configured workflow families: sdxl" in detail
    assert "SHIM_GENERATION_WORKFLOWS_JSON" in detail


def test_resolver_rejects_non_main_model_with_descriptive_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIM_GENERATION_MODEL_TYPES", "main")
    FakeShim.candidates = [
        {"key": "lora-id", "name": "Cinematic LoRA", "base": "sdxl", "type": "lora"},
        {"key": "main-id", "name": "Juggernaut XL", "base": "sdxl", "type": "main"},
    ]
    request = SimpleNamespace(model="lora-id")

    with pytest.raises(HTTPException) as exc_info:
        workflow_routing.resolve_requested_model(FakeShim, request, _cfg(str(tmp_path / "unused.json")))

    detail = str(exc_info.value.detail)
    assert "Cinematic LoRA (id lora-id)" in detail
    assert "type lora" in detail
    assert "type is one of: main" in detail

def test_resolver_explains_stale_uuid_and_lists_current_main_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIM_GENERATION_MODEL_TYPES", "main")
    FakeShim.candidates = [
        {"key": "current-id", "name": "Juggernaut XL v9", "base": "sdxl", "type": "main"},
    ]
    request = SimpleNamespace(model="stale-uuid")

    with pytest.raises(HTTPException) as exc_info:
        workflow_routing.resolve_requested_model(FakeShim, request, _cfg(str(tmp_path / "unused.json")))

    detail = str(exc_info.value.detail)
    assert "stale-uuid" in detail
    assert "removed or re-imported with a new UUID" in detail
    assert "Juggernaut XL v9 (id current-id)" in detail


def test_generic_flux_loader_receives_selected_model() -> None:
    graph = {
        "nodes": [
            {
                "id": "loader",
                "data": {
                    "type": "flux_model_loader",
                    "inputs": {"model": {"value": None}},
                },
            }
        ],
        "edges": [],
    }

    workflow_routing._inject_generic_model_loaders(
        graph,
        {"key": "flux-id", "name": "FLUX Main", "base": "flux", "type": "main"},
        "id",
    )

    value = graph["nodes"][0]["data"]["inputs"]["model"]["value"]
    assert value["key"] == "flux-id"
    assert value["name"] == "FLUX Main"


def test_generic_loader_adds_value_missing_from_builtin_workflow_export() -> None:
    graph = {
        "nodes": [
            {
                "id": "loader",
                "data": {
                    "type": "main_model_loader",
                    "inputs": {"model": {"name": "model", "label": ""}},
                },
            }
        ],
        "edges": [],
    }

    workflow_routing._inject_generic_model_loaders(
        graph,
        {"key": "sd-id", "name": "Dreamshaper 8", "base": "sd-1", "type": "main"},
        "id",
    )

    model_field = graph["nodes"][0]["data"]["inputs"]["model"]
    assert model_field["name"] == "model"
    assert model_field["value"]["key"] == "sd-id"


def _loader_graph(node_type: str, fields: tuple[str, ...]) -> dict:
    return {
        "nodes": [
            {
                "id": "loader",
                "data": {
                    "type": node_type,
                    "inputs": {field: {"name": field} for field in fields},
                },
            }
        ],
        "edges": [],
    }


def _loader_value(graph: dict, field: str):
    return graph["nodes"][0]["data"]["inputs"][field]["value"]


def test_flux_auxiliary_models_are_resolved_from_catalog() -> None:
    graph = _loader_graph(
        "flux_model_loader",
        ("model", "t5_encoder_model", "clip_embed_model", "vae_model"),
    )
    FakeShim.candidates = [
        {"key": "t5-full", "name": "T5 Full", "type": "t5_encoder"},
        {
            "key": "t5-int8",
            "name": "T5 int8",
            "type": "t5_encoder",
            "format": "bnb_quantized_int8b",
        },
        {"key": "clip", "name": "CLIP-L", "type": "clip_embed"},
        {"key": "vae", "name": "FLUX VAE", "base": "flux", "type": "vae"},
    ]

    workflow_routing._inject_family_auxiliary_models(
        FakeShim,
        graph,
        {"key": "flux-main", "name": "FLUX.1 Dev", "base": "flux", "type": "main"},
        _cfg("unused"),
        "id",
    )

    assert _loader_value(graph, "t5_encoder_model")["key"] == "t5-int8"
    assert _loader_value(graph, "clip_embed_model")["key"] == "clip"
    assert _loader_value(graph, "vae_model")["key"] == "vae"


def test_quantized_flux2_resolves_matching_qwen3_and_vae() -> None:
    graph = _loader_graph(
        "flux2_klein_model_loader",
        ("model", "vae_model", "qwen3_encoder_model", "qwen3_source_model"),
    )
    FakeShim.candidates = [
        {"key": "flux2-vae", "name": "FLUX.2 VAE", "base": "flux2", "type": "vae"},
        {
            "key": "qwen4",
            "name": "Qwen3 4B",
            "type": "qwen3_encoder",
            "variant": "qwen3_4b",
        },
        {
            "key": "qwen8",
            "name": "Qwen3 8B",
            "type": "qwen3_encoder",
            "variant": "qwen3_8b",
        },
    ]

    workflow_routing._inject_family_auxiliary_models(
        FakeShim,
        graph,
        {
            "key": "flux2-main",
            "name": "FLUX.2 Klein 4B GGUF",
            "base": "flux2",
            "type": "main",
            "format": "gguf_quantized",
            "variant": "klein_4b",
        },
        _cfg("unused"),
        "id",
    )

    assert _loader_value(graph, "vae_model")["key"] == "flux2-vae"
    assert _loader_value(graph, "qwen3_encoder_model")["key"] == "qwen4"


def test_diffusers_z_image_uses_selected_model_as_component_source() -> None:
    graph = _loader_graph(
        "z_image_model_loader",
        ("model", "vae_model", "qwen3_encoder_model", "qwen3_source_model"),
    )
    model = {
        "key": "z-image-main",
        "name": "Z-Image Turbo",
        "base": "z-image",
        "type": "main",
        "format": "diffusers",
    }
    FakeShim.candidates = []

    workflow_routing._inject_family_auxiliary_models(
        FakeShim,
        graph,
        model,
        _cfg("unused"),
        "id",
    )

    assert _loader_value(graph, "qwen3_source_model")["key"] == "z-image-main"
