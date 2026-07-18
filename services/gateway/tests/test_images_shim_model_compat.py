from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


IMAGES_APP = Path(__file__).resolve().parents[2] / "images" / "app"
sys.path.insert(0, str(IMAGES_APP))
import model_compat  # noqa: E402


def _write_graph(tmp_path: Path, node_type: str) -> str:
    path = tmp_path / "workflow.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "prompt",
                        "data": {
                            "type": node_type,
                            "inputs": {},
                        },
                    }
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


class FakeShim:
    candidates: list[dict] = []

    @classmethod
    def _list_invokeai_models(cls, *, cfg):
        return "fake://models", list(cls.candidates)

    @staticmethod
    def _is_generation_model_candidate(candidate):
        return str(candidate.get("type") or "main").lower() in {"main", "checkpoint"}

    @staticmethod
    def _candidate_strings(candidate):
        return [
            str(candidate.get(key) or "").strip()
            for key in ("key", "id", "name", "model_name")
            if str(candidate.get(key) or "").strip()
        ]

    @staticmethod
    def _normalize_invokeai_candidate(candidate):
        return dict(candidate)


def test_detects_sdxl_workflow(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_compel_prompt")
    assert model_compat.detect_graph_model_family(graph_path) == "sdxl"


def test_backend_default_selects_compatible_sdxl_model(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_compel_prompt")
    FakeShim.candidates = [
        {"key": "sd15", "name": "Stable Diffusion 1.5", "base": "sd-1", "type": "main"},
        {"key": "juggernaut-xl", "name": "Juggernaut XL", "base": "sdxl", "type": "main"},
    ]

    def original(model, *, cfg):
        return dict(FakeShim.candidates[0])

    resolved = model_compat.resolve_model_info_for_template(
        None,
        cfg=SimpleNamespace(default_model=None, model_presets_json=None),
        template_path=graph_path,
        shim_module=FakeShim,
        original_resolver=original,
    )

    assert resolved["key"] == "juggernaut-xl"
    assert resolved["base"] == "sdxl"


def test_incompatible_configured_default_falls_back_to_sdxl(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_compel_prompt")
    FakeShim.candidates = [
        {"key": "sd15", "name": "Stable Diffusion 1.5", "base": "sd-1", "type": "main"},
        {"key": "sdxl-base", "name": "SDXL Base", "base": "sdxl", "type": "main"},
    ]

    def original(model, *, cfg):
        return dict(FakeShim.candidates[0])

    resolved = model_compat.resolve_model_info_for_template(
        "sd15",
        cfg=SimpleNamespace(default_model="sd15", model_presets_json=None),
        template_path=graph_path,
        shim_module=FakeShim,
        original_resolver=original,
    )

    assert resolved["key"] == "sdxl-base"


def test_configured_preset_target_is_treated_as_default(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_compel_prompt")
    FakeShim.candidates = [
        {"key": "sd15", "name": "Stable Diffusion 1.5", "base": "sd-1", "type": "main"},
        {"key": "sdxl-base", "name": "SDXL Base", "base": "sdxl", "type": "main"},
    ]

    def original(model, *, cfg):
        return dict(FakeShim.candidates[0])

    resolved = model_compat.resolve_model_info_for_template(
        "sd15",
        cfg=SimpleNamespace(
            default_model="gpu_default",
            model_presets_json=json.dumps({"gpu_default": {"model": "sd15"}}),
        ),
        template_path=graph_path,
        shim_module=FakeShim,
        original_resolver=original,
    )

    assert resolved["key"] == "sdxl-base"


def test_explicit_incompatible_model_is_rejected(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_model_loader")
    FakeShim.candidates = [
        {"key": "sd15", "name": "Stable Diffusion 1.5", "base": "sd-1", "type": "main"},
        {"key": "sdxl-base", "name": "SDXL Base", "base": "sdxl", "type": "main"},
    ]

    def original(model, *, cfg):
        return dict(FakeShim.candidates[0])

    with pytest.raises(HTTPException) as exc_info:
        model_compat.resolve_model_info_for_template(
            "sd15",
            cfg=SimpleNamespace(default_model="other", model_presets_json=None),
            template_path=graph_path,
            shim_module=FakeShim,
            original_resolver=original,
        )

    assert exc_info.value.status_code == 400
    assert "requires a SDXL model" in str(exc_info.value.detail)
    assert "SDXL Base" in str(exc_info.value.detail)


def test_incompatible_resolved_default_is_rejected_when_no_sdxl_is_installed(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_compel_prompt")
    FakeShim.candidates = [
        {"key": "sd15", "name": "Stable Diffusion 1.5", "base": "sd-1", "type": "main"},
    ]

    def original(model, *, cfg):
        return dict(FakeShim.candidates[0])

    with pytest.raises(HTTPException) as exc_info:
        model_compat.resolve_model_info_for_template(
            None,
            cfg=SimpleNamespace(default_model=None, model_presets_json=None),
            template_path=graph_path,
            shim_module=FakeShim,
            original_resolver=original,
        )

    assert exc_info.value.status_code == 400
    assert "resolved model" in str(exc_info.value.detail)
    assert "SDXL" in str(exc_info.value.detail)


def test_unknown_workflow_family_preserves_original_resolution(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "custom_model_loader")
    FakeShim.candidates = []
    expected = {"key": "custom", "base": "custom"}

    def original(model, *, cfg):
        return expected

    resolved = model_compat.resolve_model_info_for_template(
        "custom",
        cfg=SimpleNamespace(default_model=None, model_presets_json=None),
        template_path=graph_path,
        shim_module=FakeShim,
        original_resolver=original,
    )

    assert resolved is expected


def test_install_wraps_module_resolver_once(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path, "sdxl_compel_prompt")
    candidates = [
        {"key": "sdxl-base", "name": "SDXL Base", "base": "sdxl", "type": "main"},
    ]

    shim = SimpleNamespace()
    shim._list_invokeai_models = lambda *, cfg: ("fake://models", list(candidates))
    shim._is_generation_model_candidate = lambda candidate: True
    shim._candidate_strings = FakeShim._candidate_strings
    shim._normalize_invokeai_candidate = FakeShim._normalize_invokeai_candidate
    shim._resolve_model_info = lambda model, *, cfg: None

    original = shim._resolve_model_info
    model_compat.install_model_compat(shim)
    wrapped = shim._resolve_model_info
    model_compat.install_model_compat(shim)

    assert wrapped is shim._resolve_model_info
    assert wrapped is not original
    resolved = wrapped(
        None,
        cfg=SimpleNamespace(
            graph_template_path=graph_path,
            default_model=None,
            model_presets_json=None,
        ),
    )
    assert resolved["key"] == "sdxl-base"


def test_wrapped_resolver_prefers_selected_template_path(tmp_path: Path) -> None:
    default_graph_path = _write_graph(tmp_path, "flux_model_loader")
    selected_graph_path = tmp_path / "edit-workflow.json"
    selected_graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "prompt",
                        "data": {"type": "sdxl_compel_prompt", "inputs": {}},
                    }
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    candidates = [
        {"key": "flux-main", "name": "FLUX Main", "base": "flux", "type": "main"},
        {"key": "sdxl-base", "name": "SDXL Base", "base": "sdxl", "type": "main"},
    ]

    shim = SimpleNamespace()
    shim._list_invokeai_models = lambda *, cfg: ("fake://models", list(candidates))
    shim._is_generation_model_candidate = lambda candidate: True
    shim._candidate_strings = FakeShim._candidate_strings
    shim._normalize_invokeai_candidate = FakeShim._normalize_invokeai_candidate
    shim._resolve_model_info = lambda model, *, cfg: None
    model_compat.install_model_compat(shim)

    resolved = shim._resolve_model_info(
        None,
        cfg=SimpleNamespace(
            graph_template_path=default_graph_path,
            default_model=None,
            model_presets_json=None,
        ),
        template_path=str(selected_graph_path),
    )

    assert resolved["key"] == "sdxl-base"
