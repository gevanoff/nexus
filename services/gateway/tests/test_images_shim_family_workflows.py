from __future__ import annotations

import json
import sys
from pathlib import Path


IMAGES_ROOT = Path(__file__).resolve().parents[2] / "images"
IMAGES_APP = IMAGES_ROOT / "app"
sys.path.insert(0, str(IMAGES_APP))
import openai_images_shim as shim  # noqa: E402


def _load(name: str) -> dict:
    return json.loads((IMAGES_ROOT / "shim" / name).read_text(encoding="utf-8"))


def _input(graph: dict, node_type: str, key: str, *, role: str | None = None):
    roles: dict[str, str] = {}
    for edge in graph.get("edges", []):
        target_handle = edge.get("targetHandle")
        if target_handle in ("positive_conditioning", "negative_conditioning"):
            roles[edge["source"]] = target_handle
    for node in graph["nodes"]:
        if node["data"]["type"] != node_type:
            continue
        if role is not None and roles.get(node["id"]) != role:
            continue
        return node["data"]["inputs"][key]["value"]
    raise AssertionError(f"missing {node_type}.{key} role={role}")


def _apply(graph: dict) -> None:
    shim._apply_invokeai_workflow_overrides(
        graph,
        prompt="requested positive prompt",
        negative_prompt="requested negative prompt",
        width=640,
        height=768,
        seed=1234,
        steps=17,
        cfg_scale=4.5,
    )


def test_sd_workflow_accepts_prompt_size_seed_and_quality_overrides() -> None:
    graph = _load("graph_template_sd.json")
    _apply(graph)

    assert _input(graph, "compel", "prompt") == "requested positive prompt"
    negative = next(
        node for node in graph["nodes"] if node["data"].get("label") == "Negative Compel Prompt"
    )
    assert negative["data"]["inputs"]["prompt"]["value"] == "requested negative prompt"
    assert _input(graph, "noise", "width") == 640
    assert _input(graph, "noise", "height") == 768
    assert _input(graph, "rand_int", "low") == 1234
    assert _input(graph, "rand_int", "high") == 1235
    assert _input(graph, "denoise_latents", "steps") == 17
    assert _input(graph, "denoise_latents", "cfg_scale") == 4.5
    assert shim._detect_output_node_id(graph) == "58c957f5-0d01-41fc-a803-b2bbf0413d4f"


def test_flux_workflow_accepts_prompt_size_seed_and_quality_overrides() -> None:
    graph = _load("graph_template_flux.json")
    _apply(graph)

    assert _input(graph, "flux_text_encoder", "prompt") == "requested positive prompt"
    assert _input(graph, "flux_denoise", "width") == 640
    assert _input(graph, "flux_denoise", "height") == 768
    assert _input(graph, "flux_denoise", "num_steps") == 17
    assert _input(graph, "flux_denoise", "guidance") == 4.5
    assert shim._detect_output_node_id(graph) == "7e5172eb-48c1-44db-a770-8fd83e1435d1"


def test_sd3_workflow_routes_positive_and_negative_prompts_by_edge_role() -> None:
    graph = _load("graph_template_sd3.json")
    _apply(graph)

    assert _input(graph, "sd3_text_encoder", "prompt", role="positive_conditioning") == "requested positive prompt"
    assert _input(graph, "sd3_text_encoder", "prompt", role="negative_conditioning") == "requested negative prompt"
    assert _input(graph, "sd3_denoise", "width") == 640
    assert _input(graph, "sd3_denoise", "height") == 768
    assert _input(graph, "sd3_denoise", "steps") == 17
    assert _input(graph, "sd3_denoise", "cfg_scale") == 4.5
    assert shim._detect_output_node_id(graph) == "9eb72af0-dd9e-4ec5-ad87-d65e3c01f48b"
