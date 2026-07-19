from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


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


model_compat, workflow_routing = _load_images_modules("model_compat", "workflow_routing")


def _write_sd_graph(tmp_path: Path, *, dict_nodes: bool = False) -> str:
    path = tmp_path / ("sd-dict.json" if dict_nodes else "sd-list.json")
    loader = {
        "id": "loader",
        "type": "main_model_loader",
        "inputs": {"model": None},
    }
    if dict_nodes:
        nodes = {"loader": loader}
    else:
        nodes = [
            {
                "id": "loader",
                "data": {
                    "type": "main_model_loader",
                    "inputs": {"model": {"value": None}},
                },
            }
        ]
    path.write_text(json.dumps({"nodes": nodes, "edges": []}), encoding="utf-8")
    return str(path)


def test_detects_sd_1x_2x_list_workflow(tmp_path: Path) -> None:
    graph_path = _write_sd_graph(tmp_path)
    assert model_compat.detect_graph_model_family(graph_path) == "sd"


def test_detects_sd_1x_2x_dict_workflow(tmp_path: Path) -> None:
    graph_path = _write_sd_graph(tmp_path, dict_nodes=True)
    assert model_compat.detect_graph_model_family(graph_path) == "sd"


def test_legacy_sd_graph_is_auto_registered(monkeypatch, tmp_path: Path) -> None:
    graph_path = _write_sd_graph(tmp_path)
    monkeypatch.delenv("SHIM_GENERATION_WORKFLOWS_JSON", raising=False)

    specs = workflow_routing.configured_workflows(
        SimpleNamespace(graph_template_path=graph_path, output_node_id="final-output")
    )

    assert set(specs) == {"sd"}
    assert specs["sd"].path == graph_path
    assert specs["sd"].source == "SHIM_GRAPH_TEMPLATE_PATH"
    assert specs["sd"].output_node_id == "final-output"


def test_specific_family_marker_wins_over_generic_sd_loader(tmp_path: Path) -> None:
    path = tmp_path / "mixed.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "legacy", "data": {"type": "main_model_loader", "inputs": {}}},
                    {"id": "xl", "data": {"type": "sdxl_model_loader", "inputs": {}}},
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )

    assert model_compat.detect_graph_model_family(str(path)) == "sdxl"
