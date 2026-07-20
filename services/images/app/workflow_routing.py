from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException

from app import model_compat


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class WorkflowSpec:
    family: str
    path: str
    output_node_id: Optional[str] = None
    source: str = "SHIM_GENERATION_WORKFLOWS_JSON"


def _normalize_family(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "stable-diffusion": "sd",
        "stable-diffusion-1": "sd",
        "stable-diffusion-2": "sd",
        "sd1": "sd",
        "sd-1": "sd",
        "sd2": "sd",
        "sd-2": "sd",
        "stable-diffusion-xl": "sdxl",
        "sd-xl": "sdxl",
        "stable-diffusion-3": "sd3",
        "sd-3": "sd3",
        "flux-2": "flux2",
        "zimage": "z-image",
    }
    return aliases.get(raw, raw)


def _parse_workflow_entry(family: str, value: Any) -> Optional[WorkflowSpec]:
    normalized_family = _normalize_family(family)
    if not normalized_family:
        return None
    if isinstance(value, str):
        path = value.strip()
        if not path:
            return None
        return WorkflowSpec(family=normalized_family, path=path)
    if not isinstance(value, dict):
        return None
    path = str(value.get("path") or value.get("template") or value.get("graph") or "").strip()
    if not path:
        return None
    output_node_id = str(value.get("output_node_id") or "").strip() or None
    return WorkflowSpec(
        family=normalized_family,
        path=path,
        output_node_id=output_node_id,
    )


def configured_workflows(cfg: Any) -> Dict[str, WorkflowSpec]:
    specs: Dict[str, WorkflowSpec] = {}
    raw = str(os.getenv("SHIM_GENERATION_WORKFLOWS_JSON") or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Image workflow configuration is invalid: SHIM_GENERATION_WORKFLOWS_JSON "
                    f"must be a JSON object ({type(exc).__name__}: {exc})."
                ),
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=503,
                detail="Image workflow configuration is invalid: SHIM_GENERATION_WORKFLOWS_JSON must be a JSON object.",
            )
        for family, value in payload.items():
            spec = _parse_workflow_entry(str(family), value)
            if spec is not None:
                specs[spec.family] = spec

    default_path = str(getattr(cfg, "graph_template_path", "") or "").strip()
    cache_key = getattr(configured_workflows, "_nexus_default_family_path", None)
    default_family = getattr(configured_workflows, "_nexus_default_family", None)
    if default_path and cache_key != default_path:
        default_family = model_compat.detect_graph_model_family(default_path)
        setattr(configured_workflows, "_nexus_default_family_path", default_path)
        setattr(configured_workflows, "_nexus_default_family", default_family)
    if default_path and default_family and default_family not in specs:
        specs[default_family] = WorkflowSpec(
            family=default_family,
            path=default_path,
            output_node_id=str(getattr(cfg, "output_node_id", "") or "").strip() or None,
            source="SHIM_GRAPH_TEMPLATE_PATH",
        )
    return specs


def _allowed_model_types() -> set[str]:
    raw = str(os.getenv("SHIM_GENERATION_MODEL_TYPES") or "main").strip().lower()
    values = {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}
    return values or {"main"}


def _candidate_type(candidate: Dict[str, Any]) -> str:
    return str(candidate.get("type") or candidate.get("model_type") or "").strip().lower()


def _candidate_strings(shim_module: Any, candidate: Dict[str, Any]) -> list[str]:
    try:
        return list(shim_module._candidate_strings(candidate))
    except Exception:
        return [
            str(candidate.get(key) or "").strip()
            for key in ("key", "id", "model_key", "name", "model", "model_name", "hash")
            if str(candidate.get(key) or "").strip()
        ]


def _score_candidate(shim_module: Any, candidate: Dict[str, Any], requested: str) -> int:
    needle = requested.strip()
    needle_lower = needle.lower()
    best = 0
    for value in _candidate_strings(shim_module, candidate):
        if value == needle:
            best = max(best, 100)
        value_lower = value.lower()
        if value_lower == needle_lower:
            best = max(best, 90)
        if needle_lower and (needle_lower in value_lower or value_lower in needle_lower):
            best = max(best, 60)
    return best


def _normalize_candidate(shim_module: Any, candidate: Dict[str, Any]) -> Dict[str, Any]:
    try:
        normalized = shim_module._normalize_invokeai_candidate(candidate)
    except Exception:
        normalized = dict(candidate)
    return normalized if isinstance(normalized, dict) else dict(candidate)


def _model_label(candidate: Optional[Dict[str, Any]]) -> str:
    if not isinstance(candidate, dict):
        return "unknown model"
    name = str(candidate.get("name") or candidate.get("model_name") or candidate.get("model") or "").strip()
    key = str(candidate.get("key") or candidate.get("id") or candidate.get("model_key") or "").strip()
    if name and key and name != key:
        return f"{name} (id {key})"
    return name or key or "unknown model"


def _preset_target(shim_module: Any, cfg: Any, requested: str) -> str:
    if not requested:
        return ""
    try:
        presets = shim_module._parse_model_presets(getattr(cfg, "model_presets_json", None))
    except Exception:
        presets = {}
    preset = presets.get(requested.lower()) if isinstance(presets, dict) else None
    if isinstance(preset, str):
        return preset.strip() or requested
    if isinstance(preset, dict):
        target = str(preset.get("model") or preset.get("upstream_model") or "").strip()
        return target or requested
    return requested


def _list_candidates(shim_module: Any, cfg: Any) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    try:
        _source, raw_candidates = shim_module._list_invokeai_models(cfg=cfg)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not query the InvokeAI model catalog: {type(exc).__name__}: {exc}",
        ) from exc
    all_candidates = [item for item in (raw_candidates or []) if isinstance(item, dict)]
    allowed_types = _allowed_model_types()
    generation_candidates = [item for item in all_candidates if _candidate_type(item) in allowed_types]
    return all_candidates, generation_candidates


def _workflow_compatible_fallback(
    shim_module: Any,
    cfg: Any,
    candidates: list[Dict[str, Any]],
) -> Dict[str, Any]:
    specs = configured_workflows(cfg)
    for candidate in candidates:
        normalized = _normalize_candidate(shim_module, candidate)
        family = _normalize_family(model_compat.model_family(normalized))
        if family and family in specs:
            return normalized
    return _normalize_candidate(shim_module, candidates[0])


def resolve_requested_model(shim_module: Any, req: Any, cfg: Any) -> Optional[Dict[str, Any]]:
    explicit = str(getattr(req, "model", "") or "").strip()
    configured_default = str(getattr(cfg, "default_model", "") or "").strip()
    requested_alias = explicit or configured_default
    requested = _preset_target(shim_module, cfg, requested_alias)

    all_candidates, generation_candidates = _list_candidates(shim_module, cfg)
    if not generation_candidates:
        allowed = ", ".join(sorted(_allowed_model_types()))
        raise HTTPException(
            status_code=400,
            detail=(
                "InvokeAI has no selectable base generation models. The Image UI only lists "
                f"model type(s): {allowed}. Import a base generation model with one of these types in InvokeAI, then refresh the catalog."
            ),
        )

    if requested:
        ranked = sorted(
            ((candidate, _score_candidate(shim_module, candidate, requested)) for candidate in generation_candidates),
            key=lambda item: item[1],
            reverse=True,
        )
        if ranked and ranked[0][1] >= 60:
            return _normalize_candidate(shim_module, ranked[0][0])

        raw_ranked = sorted(
            ((candidate, _score_candidate(shim_module, candidate, requested)) for candidate in all_candidates),
            key=lambda item: item[1],
            reverse=True,
        )
        if raw_ranked and raw_ranked[0][1] >= 60:
            candidate = _normalize_candidate(shim_module, raw_ranked[0][0])
            if explicit:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Selected InvokeAI model {_model_label(candidate)} has type "
                        f"{_candidate_type(candidate) or 'unknown'}, not a base generation model. "
                        f"Choose a model whose InvokeAI type is one of: {', '.join(sorted(_allowed_model_types()))}. "
                        "LoRA, VAE, ControlNet, IP-Adapter, embedding, and similar auxiliary models must be selected through workflow-specific controls."
                    ),
                )

        if explicit:
            available = ", ".join(_model_label(_normalize_candidate(shim_module, item)) for item in generation_candidates[:12])
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Selected InvokeAI model identifier {explicit!r} is not an installed base generation model (allowed type(s): {', '.join(sorted(_allowed_model_types()))}). "
                    "It may be stale because the model was removed or re-imported with a new UUID. "
                    f"Refresh Backends and select a current model. Installed base models: {available or 'none'}."
                ),
            )

        logger.warning(
            "Configured InvokeAI default model %r could not be resolved; using first installed model of type(s): %s",
            requested,
            ", ".join(sorted(_allowed_model_types())),
        )

    return _workflow_compatible_fallback(shim_module, cfg, generation_candidates)


def select_workflow(model_info: Optional[Dict[str, Any]], cfg: Any) -> WorkflowSpec:
    specs = configured_workflows(cfg)
    family = _normalize_family(model_compat.model_family(model_info))
    default_path = str(getattr(cfg, "graph_template_path", "") or "").strip()
    default_family = _normalize_family(model_compat.detect_graph_model_family(default_path))

    if family and family in specs:
        spec = specs[family]
    elif not family and default_path:
        spec = WorkflowSpec(
            family=default_family or "unknown",
            path=default_path,
            output_node_id=str(getattr(cfg, "output_node_id", "") or "").strip() or None,
            source="SHIM_GRAPH_TEMPLATE_PATH",
        )
    else:
        configured = ", ".join(sorted(specs)) or "none"
        raise HTTPException(
            status_code=400,
            detail=(
                "No compatible InvokeAI generation workflow is configured for the selected model. "
                f"Selected model: {_model_label(model_info)}. Detected model family: {family or 'unknown'}. "
                f"Configured workflow families: {configured}. The legacy SHIM_GRAPH_TEMPLATE_PATH workflow "
                f"is {default_family.upper() if default_family else 'of unknown family'}. Configure "
                "SHIM_GENERATION_WORKFLOWS_JSON with a workflow exported for this model family, or choose a model "
                "from a configured family."
            ),
        )

    path = Path(spec.path)
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                f"The {spec.family.upper()} InvokeAI workflow is configured but its graph file does not exist: "
                f"{spec.path}. Verify the workflow mount and SHIM_GENERATION_WORKFLOWS_JSON."
            ),
        )

    graph_family = _normalize_family(model_compat.detect_graph_model_family(spec.path))
    if family and graph_family and graph_family != family:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Image workflow configuration mismatch: model {_model_label(model_info)} is {family.upper()}, "
                f"but workflow {spec.path} appears to be {graph_family.upper()}. Export or configure a matching "
                "InvokeAI workflow before retrying."
            ),
        )
    return spec


def _model_value(model_info: Dict[str, Any], mode: str) -> Any:
    key = str(model_info.get("key") or model_info.get("id") or model_info.get("model_key") or "").strip()
    name = str(model_info.get("name") or model_info.get("model") or model_info.get("model_name") or "").strip()
    if mode == "name":
        return name or key
    value: Dict[str, Any] = {}
    if key:
        value["key"] = key
    for src, dst in (("hash", "hash"), ("name", "name"), ("base", "base"), ("type", "type")):
        raw = str(model_info.get(src) or "").strip()
        if raw:
            value[dst] = raw
    return value or name or key


def _set_workflow_input(inputs: Any, field: str, value: Any) -> bool:
    if not isinstance(inputs, dict) or field not in inputs:
        return False
    current = inputs.get(field)
    if isinstance(current, dict):
        # InvokeAI's built-in exports omit ``value`` for unconfigured required
        # model fields but retain UI metadata such as ``name`` and ``label``.
        # Add the value alongside that metadata so export-to-API conversion does
        # not discard the selected model.
        current["value"] = value
    else:
        inputs[field] = {"value": value}
    return True


def _inject_generic_model_loaders(graph: Dict[str, Any], model_info: Optional[Dict[str, Any]], mode: str) -> None:
    if not isinstance(model_info, dict):
        return
    value = _model_value(model_info, mode)
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        iterable: Iterable[Dict[str, Any]] = [item for item in nodes if isinstance(item, dict)]
        for node in iterable:
            data = node.get("data")
            if not isinstance(data, dict):
                continue
            node_type = str(data.get("type") or "").strip().lower()
            if not node_type.endswith("model_loader") or any(token in node_type for token in ("vae", "lora", "refiner")):
                continue
            _set_workflow_input(data.get("inputs"), "model", value)
        return

    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "").strip().lower()
            if not node_type.endswith("model_loader") or any(token in node_type for token in ("vae", "lora", "refiner")):
                continue
            inputs = node.get("inputs")
            if isinstance(inputs, dict) and "model" in inputs:
                inputs["model"] = value
            elif "model" in node:
                node["model"] = value


def _candidate_value(candidate: Dict[str, Any], mode: str) -> Any:
    return _model_value(candidate, mode)


def _select_auxiliary_candidate(
    candidates: list[Dict[str, Any]],
    *,
    model_type: str,
    base: Optional[str] = None,
    variant: Optional[str] = None,
    prefer_quantized: bool = False,
) -> Optional[Dict[str, Any]]:
    matches = [
        item
        for item in candidates
        if _candidate_type(item) == model_type
        and (base is None or _normalize_family(item.get("base")) == _normalize_family(base))
        and (variant is None or str(item.get("variant") or "").strip().lower() == variant.lower())
    ]
    if not matches:
        return None
    if prefer_quantized:
        matches.sort(
            key=lambda item: (
                "quant" not in str(item.get("format") or "").lower()
                and "int8" not in str(item.get("name") or "").lower(),
                str(item.get("name") or "").lower(),
            )
        )
    return matches[0]


def _set_loader_input(graph: Dict[str, Any], node_type: str, field: str, value: Any) -> None:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        data = node.get("data") if isinstance(node, dict) else None
        if not isinstance(data, dict) or str(data.get("type") or "").strip().lower() != node_type:
            continue
        _set_workflow_input(data.get("inputs"), field, value)


def _inject_family_auxiliary_models(
    shim_module: Any,
    graph: Dict[str, Any],
    model_info: Optional[Dict[str, Any]],
    cfg: Any,
    mode: str,
) -> None:
    if not isinstance(model_info, dict):
        return
    family = _normalize_family(model_compat.model_family(model_info))
    if family not in {"flux", "flux2", "z-image"}:
        return

    model_format = str(model_info.get("format") or "").strip().lower()
    if family == "flux2" and model_format == "diffusers":
        # The FLUX.2 loader extracts its VAE and Qwen3 encoder directly from a
        # Diffusers main model.
        return
    if family == "z-image" and model_format == "diffusers":
        # Unlike the FLUX.2 loader, Z-Image requires the self-contained
        # Diffusers model to be named explicitly as its component source.
        _set_loader_input(
            graph,
            "z_image_model_loader",
            "qwen3_source_model",
            _candidate_value(model_info, mode),
        )
        return

    try:
        _source, raw_candidates = shim_module._list_invokeai_models(cfg=cfg)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not resolve InvokeAI auxiliary models for {family}: {type(exc).__name__}: {exc}",
        ) from exc
    candidates = [item for item in (raw_candidates or []) if isinstance(item, dict)]

    if family == "flux":
        auxiliary = {
            "t5_encoder_model": _select_auxiliary_candidate(
                candidates, model_type="t5_encoder", prefer_quantized=True
            ),
            "clip_embed_model": _select_auxiliary_candidate(candidates, model_type="clip_embed"),
            "vae_model": _select_auxiliary_candidate(candidates, model_type="vae", base="flux"),
        }
        for field, candidate in auxiliary.items():
            if candidate is None:
                raise HTTPException(status_code=400, detail=f"FLUX workflow requires installed auxiliary model: {field}.")
            _set_loader_input(graph, "flux_model_loader", field, _candidate_value(candidate, mode))
        return

    if family == "flux2":
        variant = "qwen3_4b" if str(model_info.get("variant") or "").lower() == "klein_4b" else "qwen3_8b"
        vae = _select_auxiliary_candidate(candidates, model_type="vae", base="flux2")
        encoder = _select_auxiliary_candidate(candidates, model_type="qwen3_encoder", variant=variant)
        if vae is None or encoder is None:
            raise HTTPException(
                status_code=400,
                detail=f"Quantized FLUX.2 {variant} workflow requires a matching Qwen3 encoder and FLUX.2 VAE.",
            )
        _set_loader_input(graph, "flux2_klein_model_loader", "vae_model", _candidate_value(vae, mode))
        _set_loader_input(
            graph, "flux2_klein_model_loader", "qwen3_encoder_model", _candidate_value(encoder, mode)
        )
        return

    if family == "z-image":
        vae = _select_auxiliary_candidate(candidates, model_type="vae", base="flux")
        encoder = _select_auxiliary_candidate(candidates, model_type="qwen3_encoder", variant="qwen3_4b")
        if vae is None or encoder is None:
            raise HTTPException(
                status_code=400,
                detail="Quantized Z-Image workflow requires an installed FLUX VAE and Qwen3 4B encoder.",
            )
        _set_loader_input(graph, "z_image_model_loader", "vae_model", _candidate_value(vae, mode))
        _set_loader_input(graph, "z_image_model_loader", "qwen3_encoder_model", _candidate_value(encoder, mode))


def _replace_models_route(shim_module: Any) -> None:
    app = shim_module.app
    original_list_models = shim_module.list_models
    app.router.routes = [
        route
        for route in app.router.routes
        if not (getattr(route, "path", None) == "/v1/models" and "GET" in (getattr(route, "methods", set()) or set()))
    ]

    def list_generation_models(raw: bool = False) -> Dict[str, Any]:
        cfg = shim_module._get_config()
        source_url: Optional[str] = None
        upstream: list[Dict[str, Any]] = []
        upstream_error: Optional[str] = None
        try:
            source_url, upstream = shim_module._list_invokeai_models(cfg=cfg)
        except Exception as exc:
            upstream_error = str(getattr(exc, "detail", exc))

        specs: Dict[str, WorkflowSpec] = {}
        workflow_error: Optional[str] = None
        try:
            specs = configured_workflows(cfg)
        except HTTPException as exc:
            workflow_error = str(exc.detail)

        data: list[Dict[str, Any]] = []
        seen: set[str] = set()
        allowed = _allowed_model_types()
        for candidate in upstream or []:
            if not isinstance(candidate, dict) or _candidate_type(candidate) not in allowed:
                continue
            normalized = _normalize_candidate(shim_module, candidate)
            model_id = str(normalized.get("key") or normalized.get("id") or "").strip()
            if not model_id or model_id.lower() in seen:
                continue
            seen.add(model_id.lower())
            raw_name = str(normalized.get("name") or model_id).strip()
            family = _normalize_family(model_compat.model_family(normalized)) or "unknown"
            configured = family in specs
            suffix = family.upper()
            if not configured:
                suffix += "; workflow not configured"
            entry: Dict[str, Any] = {
                "id": model_id,
                "object": "model",
                "owned_by": "invokeai",
                "name": f"{raw_name} — {suffix}",
                "metadata": {
                    "name": raw_name,
                    "base": str(normalized.get("base") or "").strip(),
                    "type": _candidate_type(normalized),
                    "family": family,
                    "workflow_configured": configured,
                },
            }
            if configured:
                entry["metadata"]["workflow_source"] = specs[family].source
            data.append(entry)

        response: Dict[str, Any] = {"object": "list", "data": data}
        if raw:
            original = original_list_models(raw=True)
            response["shim"] = {
                "invokeai_base_url": cfg.invokeai_base_url,
                "source_url": source_url,
                "upstream_error": upstream_error,
                "workflow_error": workflow_error,
                "generation_model_types": sorted(allowed),
                "configured_workflows": {
                    family: {
                        "path": spec.path,
                        "output_node_id": spec.output_node_id,
                        "source": spec.source,
                    }
                    for family, spec in specs.items()
                },
                "upstream_models_raw": upstream,
                "legacy_catalog": original,
            }
        return response

    app.add_api_route("/v1/models", list_generation_models, methods=["GET"], name="list_generation_models")
    shim_module.list_models = list_generation_models


def install_workflow_routing(shim_module: Any) -> None:
    if getattr(shim_module, "_nexus_workflow_routing_installed", False):
        return

    original_generate = shim_module._invokeai_generate_b64
    original_overrides = shim_module._apply_invokeai_workflow_overrides

    def workflow_aware_overrides(graph: Dict[str, Any], **kwargs: Any) -> None:
        original_overrides(graph, **kwargs)
        mode = str(kwargs.get("model_input_mode") or "id").strip().lower()
        _inject_generic_model_loaders(
            graph,
            kwargs.get("model_info"),
            mode,
        )
        _inject_family_auxiliary_models(
            shim_module,
            graph,
            kwargs.get("model_info"),
            kwargs.get("cfg"),
            mode,
        )

    def workflow_aware_generate(req: Any, *, cfg: Any) -> str:
        model_info = resolve_requested_model(shim_module, req, cfg)
        spec = select_workflow(model_info, cfg)
        selected_id = str(model_info.get("key") or model_info.get("id") or model_info.get("name") or "").strip() if isinstance(model_info, dict) else ""
        routed_req = req.model_copy(update={"model": selected_id}) if selected_id and hasattr(req, "model_copy") else req
        routed_cfg = replace(
            cfg,
            graph_template_path=spec.path,
            output_node_id=spec.output_node_id or getattr(cfg, "output_node_id", None),
        )
        logger.info(
            "Selected InvokeAI workflow family=%s path=%s model=%s",
            spec.family,
            spec.path,
            _model_label(model_info),
        )
        return original_generate(routed_req, cfg=routed_cfg)

    shim_module._apply_invokeai_workflow_overrides = workflow_aware_overrides
    shim_module._invokeai_generate_b64 = workflow_aware_generate
    _replace_models_route(shim_module)
    shim_module._nexus_workflow_routing_installed = True
