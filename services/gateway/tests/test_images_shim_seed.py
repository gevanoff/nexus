from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


IMAGES_APP = Path(__file__).resolve().parents[2] / "images" / "app"
sys.path.insert(0, str(IMAGES_APP))
import openai_images_shim as shim  # noqa: E402


def _seed_graph() -> dict:
    return {
        "nodes": [
            {
                "data": {
                    "type": "rand_int",
                    "label": "Random Seed",
                    "inputs": {
                        "low": {"value": 0},
                        "high": {"value": 2_147_483_647},
                    },
                }
            }
        ]
    }


def _apply(graph: dict, seed: int | None) -> None:
    shim._apply_invokeai_workflow_overrides(
        graph,
        prompt="test",
        negative_prompt="",
        width=512,
        height=512,
        seed=seed,
    )


def test_fixed_seed_uses_single_value_randint_interval() -> None:
    graph = _seed_graph()
    _apply(graph, 1234)
    inputs = graph["nodes"][0]["data"]["inputs"]
    assert inputs["low"]["value"] == 1234
    assert inputs["high"]["value"] == 1235


def test_missing_seed_preserves_random_interval() -> None:
    graph = _seed_graph()
    _apply(graph, None)
    inputs = graph["nodes"][0]["data"]["inputs"]
    assert inputs["low"]["value"] == 0
    assert inputs["high"]["value"] == 2_147_483_647


def test_seeded_batch_uses_distinct_consecutive_seeds(monkeypatch) -> None:
    seen: list[int | None] = []

    monkeypatch.setattr(
        shim,
        "_get_config",
        lambda: SimpleNamespace(mode="invokeai_queue"),
    )

    def fake_generate(request, *, cfg):
        seen.append(request.seed)
        return "image"

    monkeypatch.setattr(shim, "_invokeai_generate_b64", fake_generate)
    response = shim.images_generations(
        shim.ImagesGenerationsRequest(prompt="test", n=4, seed=1200)
    )
    assert seen == [1200, 1201, 1202, 1203]
    assert len(response["data"]) == 4
