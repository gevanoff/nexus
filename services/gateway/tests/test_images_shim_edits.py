from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


IMAGES_APP = Path(__file__).resolve().parents[2] / "images" / "app"
sys.path.insert(0, str(IMAGES_APP))
import openai_images_edits as edits  # noqa: E402


def _workflow_node(node_id: str, node_type: str, **inputs):
    return {
        "id": node_id,
        "data": {
            "type": node_type,
            "inputs": {key: {"value": value} for key, value in inputs.items()},
        },
    }


def test_image_to_image_injects_reference_and_denoise_strength() -> None:
    graph = {
        "nodes": [
            _workflow_node("source", "image_to_latents", image=None),
            _workflow_node("denoise", "denoise_latents", denoising_start=0.0),
        ],
        "edges": [],
    }

    result = edits.inject_reference_inputs(
        graph,
        purpose=edits.PURPOSE_IMAGE_TO_IMAGE,
        image_name="reference.png",
        strength=0.65,
    )

    assert graph["nodes"][0]["data"]["inputs"]["image"]["value"] == {"image_name": "reference.png"}
    assert graph["nodes"][1]["data"]["inputs"]["denoising_start"]["value"] == pytest.approx(0.35)
    assert result["image_inputs_updated"] == 1
    assert result["strength_inputs_updated"] == 1


@pytest.mark.parametrize("purpose", [edits.PURPOSE_COMPOSITION, edits.PURPOSE_STYLE])
def test_ip_adapter_purposes_inject_reference_and_weight(purpose: str) -> None:
    graph = {
        "nodes": [
            _workflow_node("adapter", "ip_adapter", image=None, weight=0.0),
        ],
        "edges": [],
    }

    result = edits.inject_reference_inputs(
        graph,
        purpose=purpose,
        image_name="reference.webp",
        strength=0.4,
    )

    inputs = graph["nodes"][0]["data"]["inputs"]
    assert inputs["image"]["value"] == {"image_name": "reference.webp"}
    assert inputs["weight"]["value"] == pytest.approx(0.4)
    assert result["matched_nodes"] == ["adapter"]


def test_controlnet_injects_control_image_and_weight() -> None:
    graph = {
        "nodes": {
            "control": {
                "id": "control",
                "type": "controlnet",
                "inputs": {
                    "control_image": None,
                    "control_weight": 0.0,
                },
            }
        },
        "edges": [],
    }

    edits.inject_reference_inputs(
        graph,
        purpose=edits.PURPOSE_CONTROLNET,
        image_name="control.png",
        strength=0.8,
    )

    inputs = graph["nodes"]["control"]["inputs"]
    assert inputs["control_image"] == {"image_name": "control.png"}
    assert inputs["control_weight"] == pytest.approx(0.8)


def test_inpainting_injects_mask() -> None:
    graph = {
        "nodes": [
            _workflow_node("source", "image_to_latents", image=None),
            _workflow_node("denoise", "denoise_latents", denoise_strength=0.0),
            _workflow_node("mask", "create_denoise_mask", mask=None),
        ],
        "edges": [],
    }

    result = edits.inject_reference_inputs(
        graph,
        purpose=edits.PURPOSE_IMAGE_TO_IMAGE,
        image_name="reference.png",
        strength=0.7,
        mask_name="mask.png",
    )

    assert graph["nodes"][2]["data"]["inputs"]["mask"]["value"] == {"image_name": "mask.png"}
    assert result["mask_inputs_updated"] == 1


def test_mask_is_rejected_for_non_inpainting_purpose() -> None:
    with pytest.raises(HTTPException) as exc:
        edits.validate_edit_request(
            purpose=edits.PURPOSE_STYLE,
            strength=0.5,
            has_mask=True,
        )

    assert exc.value.status_code == 400
    assert "image_to_image" in str(exc.value.detail)


def test_graph_without_purpose_nodes_fails_closed() -> None:
    graph = {
        "nodes": [
            _workflow_node("prompt", "string", value="hello"),
            _workflow_node("output", "image_output", image=None),
        ],
        "edges": [],
    }

    with pytest.raises(HTTPException) as exc:
        edits.inject_reference_inputs(
            graph,
            purpose=edits.PURPOSE_CONTROLNET,
            image_name="reference.png",
            strength=0.5,
        )

    assert exc.value.status_code == 400
    assert "ControlNet" in str(exc.value.detail) or "controlnet" in str(exc.value.detail).lower()


def test_graph_without_strength_input_fails_closed() -> None:
    graph = {
        "nodes": [
            _workflow_node("adapter", "ip_adapter", image=None),
        ],
        "edges": [],
    }

    with pytest.raises(HTTPException) as exc:
        edits.inject_reference_inputs(
            graph,
            purpose=edits.PURPOSE_STYLE,
            image_name="reference.png",
            strength=0.5,
        )

    assert exc.value.status_code == 400
    assert "strength" in str(exc.value.detail).lower()


def test_normalize_purpose_accepts_common_aliases() -> None:
    assert edits.normalize_purpose("img2img") == edits.PURPOSE_IMAGE_TO_IMAGE
    assert edits.normalize_purpose("style_reference") == edits.PURPOSE_STYLE
    assert edits.normalize_purpose("control_net") == edits.PURPOSE_CONTROLNET


def test_strength_range_is_validated() -> None:
    assert edits.normalize_strength("0.75") == pytest.approx(0.75)
    with pytest.raises(HTTPException):
        edits.normalize_strength("1.1")
