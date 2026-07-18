from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from fastapi import HTTPException


logger = logging.getLogger("uvicorn.error")


_GRAPH_FAMILY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "sdxl",
        (
            "sdxl_model_loader",
            "sdxl_compel_prompt",
            "sdxl_refiner_model_loader",
            "sdxl_lora_loader",
        ),
    ),
    (
        "flux",
        (
            "flux_model_loader",
            "flux_text_encoder",
            "flux_denoise",
            "flux_ip_adapter",
        ),
    ),
    (
        "sd3",
        (
            "sd3_model_loader",
            "sd3_text_encoder",
            "sd3_denoise",
        ),
    ),
)


def _iter_graph_node_types(graph: Dict[str, Any]) -> Iterable[str]:
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            data = node.get("data")
            if not isinstance(data, dict):
                continue
            node_type = str(data.get("type") or "").strip().lower()
            if node_type:
                yield node_type
        return

    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "").strip().lower()
            if node_type:
                yield node_type


def detect_graph_model_family(path: Optional[str]) -> Optional[str]:
    template_path = str(path or "").strip()
    if not template_path:
        return None
    try:
        graph = json.loads(Path(template_path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "Could not inspect InvokeAI graph family path=%s error=%s: %s",
            template_path,
            type(exc).__name__,
            exc,
        )
        return None
    if not isinstance(graph, dict):
        return None

    node_types = tuple(_iter_graph_node_types(graph))
    for family, markers in _GRAPH_FAMILY_MARKERS:
        if any(any(marker in node_type for marker in markers) for node_type in node_types):
            return family
    return None


def _family_from_text(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    if "sdxl" in normalized or "stable-diffusion-xl" in normalized:
        return "sdxl"
    if re.search(r"(^|-)xl($|-)", normalized):
        return "sdxl"
    if "flux" in normalized:
        return "flux"
    if re.search(r"(^|-)sd-?3($|-)", normalized) or "stable-diffusion-3" in normalized:
        return "sd3"
    if (
        re.search(r"(^|-)sd-?[12]($|-)", normalized)
        or "stable-diffusion-1" in normalized
        or "stable-diffusion-2" in normalized
    ):
        return "sd"
    return None


def model_family(model: Any) -> Optional[str]:
    if isinstance(model, dict):
        for key in ("base", "base_model"):
            family = _family_from_text(model.get(key))
            if family:
                return family
        for key in ("name", "model", "model_name", "key", "id", "model_key", "path"):
            family = _family_from_text(model.get(key))
            if family:
                return family
        return None
    return _family_from_text(model)


def _score_candidate(shim_module: Any, candidate: Dict[str, Any], requested: str) -> int:
    needle = requested.strip()
    needle_lower = needle.lower()
    best = 0
    try:
        values = shim_module._candidate_strings(candidate)
    except Exception:
        values = [
            str(candidate.get(key) or "").strip()
            for key in ("key", "id", "model_key", "name", "model", "model_name")
            if str(candidate.get(key) or "").strip()
        ]
    for value in values:
        if value == needle:
            best = max(best, 100)
        value_lower = value.lower()
        if value_lower == needle_lower:
            best = max(best, 90)
        if needle_lower and (needle_lower in value_lower or value_lower in needle_lower):
            best = max(best, 60)
    return best


def _normalized_candidate(shim_module: Any, candidate: Dict[str, Any]) -> Dict[str, Any]:
    try:
        normalized = shim_module._normalize_invokeai_candidate(candidate)
    except Exception:
        normalized = dict(candidate)
    return normalized if isinstance(normalized, dict) else dict(candidate)


def _compatible_candidates(shim_module: Any, cfg: Any, family: str) -> list[Dict[str, Any]]:
    try:
        _source, candidates = shim_module._list_invokeai_models(cfg=cfg)
    except Exception as exc:
        logger.warning(
            "InvokeAI model compatibility lookup unavailable family=%s error=%s: %s",
            family,
            type(exc).__name__,
            exc,
        )
        return []

    output: list[Dict[str, Any]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        try:
            if not shim_module._is_generation_model_candidate(candidate):
                continue
        except Exception:
            pass
        if model_family(candidate) == family:
            output.append(candidate)
    return output


def _configured_default_targets(cfg: Any) -> set[str]:
    targets: set[str] = set()
    configured_default = str(getattr(cfg, "default_model", "") or "").strip()
    if configured_default:
        targets.add(configured_default.lower())

    raw_presets = str(getattr(cfg, "model_presets_json", "") or "").strip()
    if not configured_default or not raw_presets:
        return targets
    try:
        presets = json.loads(raw_presets)
    except Exception:
        return targets
    if not isinstance(presets, dict):
        return targets
    preset = presets.get(configured_default) or presets.get(configured_default.lower())
    if isinstance(preset, str) and preset.strip():
        targets.add(preset.strip().lower())
    elif isinstance(preset, dict):
        for key in ("model", "upstream_model"):
            value = str(preset.get(key) or "").strip()
            if value:
                targets.add(value.lower())
    return targets


def resolve_model_info_for_template(
    model: Optional[str],
    *,
    cfg: Any,
    template_path: Optional[str],
    shim_module: Any,
    original_resolver: Callable[..., Optional[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    expected_family = detect_graph_model_family(template_path)
    if not expected_family:
        return original_resolver(model, cfg=cfg)

    requested = str(model or "").strip()
    requested_is_configured_default = bool(
        requested and requested.lower() in _configured_default_targets(cfg)
    )
    compatible = _compatible_candidates(shim_module, cfg, expected_family)

    if compatible:
        if requested:
            ranked = sorted(
                ((candidate, _score_candidate(shim_module, candidate, requested)) for candidate in compatible),
                key=lambda item: item[1],
                reverse=True,
            )
            if ranked and ranked[0][1] >= 60:
                selected = _normalized_candidate(shim_module, ranked[0][0])
                logger.info(
                    "Resolved InvokeAI model for workflow family=%s requested=%r selected=%r",
                    expected_family,
                    requested,
                    selected.get("key") or selected.get("name"),
                )
                return selected

            if requested_is_configured_default:
                selected = _normalized_candidate(shim_module, compatible[0])
                logger.warning(
                    "Configured InvokeAI default model %r is incompatible with workflow family=%s; selected=%r",
                    requested,
                    expected_family,
                    selected.get("key") or selected.get("name"),
                )
                return selected

            resolved = original_resolver(model, cfg=cfg)
            resolved_family = model_family(resolved)
            if resolved_family == expected_family:
                return resolved
            normalized_available = [_normalized_candidate(shim_module, item) for item in compatible[:8]]
            available = [
                str(item.get("name") or item.get("key") or "")
                for item in normalized_available
            ]
            raise HTTPException(
                status_code=400,
                detail=(
                    f"InvokeAI workflow requires a {expected_family.upper()} model, but model {requested!r} "
                    f"is unavailable or incompatible. Compatible installed models: {', '.join(v for v in available if v) or 'none'}"
                ),
            )

        selected = _normalized_candidate(shim_module, compatible[0])
        logger.info(
            "Auto-selected InvokeAI model for workflow family=%s selected=%r",
            expected_family,
            selected.get("key") or selected.get("name"),
        )
        return selected

    resolved = original_resolver(model, cfg=cfg)
    resolved_family = model_family(resolved)
    if resolved is None or resolved_family == expected_family:
        # Model discovery can be unavailable even when the graph's embedded model is valid.
        return resolved

    selected_name = ""
    if isinstance(resolved, dict):
        selected_name = str(resolved.get("name") or resolved.get("key") or resolved.get("id") or "").strip()
    selected_name = selected_name or requested or "backend default"
    raise HTTPException(
        status_code=400,
        detail=(
            f"InvokeAI workflow requires a {expected_family.upper()} model, but the resolved model "
            f"{selected_name!r} is {resolved_family or 'an unknown family'}. Select a compatible model "
            "or export a workflow matching the selected model family."
        ),
    )


def install_model_compat(shim_module: Any) -> None:
    current = shim_module._resolve_model_info
    if getattr(current, "_nexus_workflow_aware", False):
        return

    def workflow_aware_resolver(
        model: Optional[str],
        *,
        cfg: Any,
        template_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return resolve_model_info_for_template(
            model,
            cfg=cfg,
            template_path=template_path or getattr(cfg, "graph_template_path", None),
            shim_module=shim_module,
            original_resolver=current,
        )

    setattr(workflow_aware_resolver, "_nexus_workflow_aware", True)
    setattr(workflow_aware_resolver, "_nexus_original_resolver", current)
    shim_module._resolve_model_info = workflow_aware_resolver
