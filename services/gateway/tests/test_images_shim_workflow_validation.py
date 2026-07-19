from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


IMAGES_APP = Path(__file__).resolve().parents[2] / "images" / "app"
sys.path.insert(0, str(IMAGES_APP))
import workflow_routing  # noqa: E402
import workflow_validation  # noqa: E402


def _write_graph(tmp_path: Path, node_type: str) -> str:
    path = tmp_path / "workflow.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "final-output",
                        "data": {
                            "type": node_type,
                            "isIntermediate": False,
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


def test_validation_detects_output_node_for_routed_workflow(tmp_path: Path) -> None:
    path = _write_graph(tmp_path, "l2i")
    routing = SimpleNamespace(
        select_workflow=lambda model_info, cfg: workflow_routing.WorkflowSpec(
            family="flux",
            path=path,
            output_node_id=None,
        )
    )
    shim = SimpleNamespace(_detect_output_node_id=lambda graph: "final-output")

    workflow_validation.install_workflow_output_validation(routing, shim)
    spec = routing.select_workflow({}, SimpleNamespace())

    assert spec.output_node_id == "final-output"


def test_validation_requires_explicit_output_when_detection_fails(tmp_path: Path) -> None:
    path = _write_graph(tmp_path, "custom_decode")
    routing = SimpleNamespace(
        select_workflow=lambda model_info, cfg: workflow_routing.WorkflowSpec(
            family="flux",
            path=path,
            output_node_id=None,
        )
    )
    shim = SimpleNamespace(_detect_output_node_id=lambda graph: None)

    workflow_validation.install_workflow_output_validation(routing, shim)

    with pytest.raises(HTTPException) as exc_info:
        routing.select_workflow({}, SimpleNamespace())

    assert exc_info.value.status_code == 503
    detail = str(exc_info.value.detail)
    assert "output_node_id" in detail
    assert "SHIM_GENERATION_WORKFLOWS_JSON" in detail
