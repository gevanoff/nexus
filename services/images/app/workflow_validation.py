from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def install_workflow_output_validation(routing_module: Any, shim_module: Any) -> None:
    current = routing_module.select_workflow
    if getattr(current, "_nexus_output_validated", False):
        return

    def validated_select_workflow(model_info: Any, cfg: Any):
        spec = current(model_info, cfg)
        if spec.output_node_id:
            return spec

        try:
            graph = json.loads(Path(spec.path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Could not inspect the routed {spec.family.upper()} workflow output node at {spec.path}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc

        output_node_id = shim_module._detect_output_node_id(graph) if isinstance(graph, dict) else None
        if not output_node_id:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"The routed {spec.family.upper()} workflow at {spec.path} has no configured output_node_id "
                    "and the shim could not identify a final image-output node. Add output_node_id to that family's "
                    "SHIM_GENERATION_WORKFLOWS_JSON entry or export a workflow with a recognizable final image node."
                ),
            )
        return replace(spec, output_node_id=output_node_id)

    setattr(validated_select_workflow, "_nexus_output_validated", True)
    setattr(validated_select_workflow, "_nexus_original_select_workflow", current)
    routing_module.select_workflow = validated_select_workflow
