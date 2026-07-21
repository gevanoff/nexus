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
        if target_handle in (
            "positive_conditioning",
            "negative_conditioning",
            "positive_text_conditioning",
            "negative_text_conditioning",
        ):
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
        scheduler="heun",
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


def test_flux2_workflow_accepts_prompt_size_seed_and_quality_overrides() -> None:
    graph = _load("graph_template_flux2.json")
    _apply(graph)

    assert _input(
        graph,
        "flux2_klein_text_encoder",
        "prompt",
        role="positive_text_conditioning",
    ) == "requested positive prompt"
    assert _input(
        graph,
        "flux2_klein_text_encoder",
        "prompt",
        role="negative_text_conditioning",
    ) == "requested negative prompt"
    assert _input(graph, "flux2_denoise", "width") == 640
    assert _input(graph, "flux2_denoise", "height") == 768
    assert _input(graph, "flux2_denoise", "seed") == 1234
    assert _input(graph, "flux2_denoise", "num_steps") == 17
    assert _input(graph, "flux2_denoise", "cfg_scale") == 4.5
    assert _input(graph, "flux2_denoise", "scheduler") == "heun"
    assert shim._detect_output_node_id(graph) == "flux2-output"

    api_graph = shim._workflow_export_to_api_graph(graph, flatten_inputs=True)
    assert any(
        edge["destination"] == {
            "node_id": "flux2-denoise",
            "field": "negative_text_conditioning",
        }
        for edge in api_graph["edges"]
    )


def test_long_image_batch_deadlines_are_layered(monkeypatch) -> None:
    monkeypatch.delenv("SHIM_TIMEOUT_S", raising=False)
    assert shim._get_config().timeout_s == 900

    topology = json.loads(
        (IMAGES_ROOT.parents[1] / "deploy" / "topology" / "production.json").read_text(
            encoding="utf-8"
        )
    )
    gateway_timeout = float(topology["defaults"]["env"]["IMAGES_HTTP_TIMEOUT_SEC"])
    shim_timeout = float(topology["hosts"]["ada2"]["env"]["SHIM_TIMEOUT_S"])

    assert gateway_timeout > shim_timeout >= 900


def test_invokeai_step_progress_is_aggregated_across_batch() -> None:
    progress_id = "progress-test-1234"
    shim._progress_start(progress_id, 4)
    shim._progress_bind_item(progress_id, "77", 1, 4)
    shim._progress_update_from_event(
        {
            "item_id": 77,
            "percentage": 0.5,
            "message": "Denoising",
            "invocation": {"type": "sd3_denoise"},
        }
    )

    state = shim._progress_snapshot(progress_id)
    assert state is not None
    assert state["status"] == "rendering"
    assert state["percentage"] == 0.375
    assert state["message"] == "Denoising"
    assert state["current_image"] == 2
    assert state["total_images"] == 4


def test_z_image_workflow_accepts_prompt_size_seed_and_quality_overrides() -> None:
    graph = _load("graph_template_z_image.json")
    _apply(graph)

    assert _input(graph, "z_image_text_encoder", "prompt") == "requested positive prompt"
    assert _input(graph, "z_image_denoise", "width") == 640
    assert _input(graph, "z_image_denoise", "height") == 768
    assert _input(graph, "z_image_denoise", "seed") == 1234
    assert _input(graph, "z_image_denoise", "steps") == 17
    assert _input(graph, "z_image_denoise", "guidance_scale") == 4.5
    assert _input(graph, "z_image_denoise", "scheduler") == "heun"
    assert shim._detect_output_node_id(graph) == "z-image-output"
