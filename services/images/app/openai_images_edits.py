from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

try:
    from app import openai_images_shim as shim
except ImportError:  # pragma: no cover - direct module import in focused tests
    import openai_images_shim as shim  # type: ignore


router = APIRouter()
logger = logging.getLogger("uvicorn.error")

PURPOSE_IMAGE_TO_IMAGE = "image_to_image"
PURPOSE_COMPOSITION = "composition"
PURPOSE_STYLE = "style"
PURPOSE_CONTROLNET = "controlnet"
SUPPORTED_PURPOSES = {
    PURPOSE_IMAGE_TO_IMAGE,
    PURPOSE_COMPOSITION,
    PURPOSE_STYLE,
    PURPOSE_CONTROLNET,
}
_PURPOSE_ALIASES = {
    "img2img": PURPOSE_IMAGE_TO_IMAGE,
    "image-to-image": PURPOSE_IMAGE_TO_IMAGE,
    "image_to_image": PURPOSE_IMAGE_TO_IMAGE,
    "composition": PURPOSE_COMPOSITION,
    "composition_reference": PURPOSE_COMPOSITION,
    "style": PURPOSE_STYLE,
    "style_reference": PURPOSE_STYLE,
    "controlnet": PURPOSE_CONTROLNET,
    "control_net": PURPOSE_CONTROLNET,
}


@dataclass(frozen=True)
class EditSpec:
    prompt: str
    purpose: str
    strength: float
    n: int
    size: Optional[str]
    model: Optional[str]
    seed: Optional[int]
    negative_prompt: str
    steps: Optional[int]
    cfg_scale: Optional[float]
    scheduler: Optional[str]


def normalize_purpose(value: Any) -> str:
    raw = str(value or PURPOSE_IMAGE_TO_IMAGE).strip().lower().replace(" ", "_")
    purpose = _PURPOSE_ALIASES.get(raw, raw)
    if purpose not in SUPPORTED_PURPOSES:
        choices = ", ".join(sorted(SUPPORTED_PURPOSES))
        raise HTTPException(status_code=400, detail=f"purpose must be one of: {choices}")
    return purpose


def normalize_strength(value: Any) -> float:
    try:
        strength = float(0.65 if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="strength must be a number between 0 and 1") from exc
    if not 0.0 <= strength <= 1.0:
        raise HTTPException(status_code=400, detail="strength must be between 0 and 1")
    return strength


def validate_edit_request(*, purpose: str, strength: float, has_mask: bool) -> None:
    if purpose not in SUPPORTED_PURPOSES:
        raise HTTPException(status_code=400, detail=f"unsupported reference-image purpose: {purpose}")
    if not 0.0 <= strength <= 1.0:
        raise HTTPException(status_code=400, detail="strength must be between 0 and 1")
    if has_mask and purpose != PURPOSE_IMAGE_TO_IMAGE:
        raise HTTPException(
            status_code=400,
            detail="mask upload is only supported for image_to_image/inpainting workflows",
        )


def _purpose_template_path(purpose: str, *, has_mask: bool, cfg: shim.ShimConfig) -> str:
    if has_mask:
        candidates = (
            os.getenv("SHIM_INPAINT_GRAPH_TEMPLATE_PATH"),
            os.getenv("SHIM_IMG2IMG_GRAPH_TEMPLATE_PATH"),
            os.getenv("SHIM_EDIT_GRAPH_TEMPLATE_PATH"),
            cfg.graph_template_path,
        )
    elif purpose == PURPOSE_IMAGE_TO_IMAGE:
        candidates = (
            os.getenv("SHIM_IMG2IMG_GRAPH_TEMPLATE_PATH"),
            os.getenv("SHIM_EDIT_GRAPH_TEMPLATE_PATH"),
            cfg.graph_template_path,
        )
    elif purpose == PURPOSE_COMPOSITION:
        candidates = (
            os.getenv("SHIM_COMPOSITION_GRAPH_TEMPLATE_PATH"),
            os.getenv("SHIM_EDIT_GRAPH_TEMPLATE_PATH"),
            cfg.graph_template_path,
        )
    elif purpose == PURPOSE_STYLE:
        candidates = (
            os.getenv("SHIM_STYLE_GRAPH_TEMPLATE_PATH"),
            os.getenv("SHIM_EDIT_GRAPH_TEMPLATE_PATH"),
            cfg.graph_template_path,
        )
    else:
        candidates = (
            os.getenv("SHIM_CONTROLNET_GRAPH_TEMPLATE_PATH"),
            os.getenv("SHIM_EDIT_GRAPH_TEMPLATE_PATH"),
            cfg.graph_template_path,
        )
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path:
            return path
    raise HTTPException(
        status_code=503,
        detail=(
            f"No InvokeAI graph template is configured for purpose={purpose!r}. "
            "Set a purpose-specific SHIM_*_GRAPH_TEMPLATE_PATH or SHIM_EDIT_GRAPH_TEMPLATE_PATH."
        ),
    )


def _iter_graph_nodes(graph: Dict[str, Any]) -> Iterable[Tuple[str, str, Dict[str, Any], bool]]:
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        for index, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            data = node.get("data")
            if not isinstance(data, dict):
                continue
            node_type = str(data.get("type") or "").strip().lower()
            inputs = data.get("inputs")
            if not isinstance(inputs, dict):
                continue
            node_id = str(node.get("id") or data.get("id") or index)
            yield node_id, node_type, inputs, True
        return

    if isinstance(nodes, dict):
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "").strip().lower()
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                yield str(node_id), node_type, inputs, False
            else:
                yield str(node_id), node_type, node, False


def _set_existing_input(inputs: Dict[str, Any], names: Iterable[str], value: Any, *, wrapped: bool) -> int:
    changed = 0
    for name in names:
        if name not in inputs:
            continue
        current = inputs.get(name)
        if wrapped and isinstance(current, dict) and "value" in current:
            current["value"] = value
        else:
            inputs[name] = value
        changed += 1
    return changed


def _type_matches(node_type: str, markers: Iterable[str]) -> bool:
    normalized = str(node_type or "").strip().lower()
    return any(marker in normalized for marker in markers)


def inject_reference_inputs(
    graph: Dict[str, Any],
    *,
    purpose: str,
    image_name: str,
    strength: float,
    mask_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Inject uploaded InvokeAI image names into a purpose-compatible graph.

    The function only changes inputs already present in the graph. This keeps the
    shim fail-closed: a text-to-image graph cannot silently pretend to support
    IP-Adapter, ControlNet, img2img, or inpainting.
    """

    validate_edit_request(purpose=purpose, strength=strength, has_mask=bool(mask_name))
    reference = {"image_name": image_name}
    mask_reference = {"image_name": mask_name} if mask_name else None

    if purpose == PURPOSE_IMAGE_TO_IMAGE:
        image_markers = (
            "image_to_latents",
            "image-to-latents",
            "img2img",
            "image_to_image",
            "initial_image",
            "i2l",
            "load_image",
        )
        image_fields = ("image", "image_name", "source_image", "initial_image", "init_image")
        strength_markers = (*image_markers, "denoise_latents")
        direct_strength_fields = ("strength", "denoise_strength", "denoising_strength")
    elif purpose in {PURPOSE_COMPOSITION, PURPOSE_STYLE}:
        image_markers = (
            "ip_adapter",
            "ipadapter",
            "flux_ip_adapter",
            "reference_image",
            "image_prompt",
        )
        image_fields = ("image", "image_name", "ip_adapter_image", "reference_image")
        strength_markers = image_markers
        direct_strength_fields = ("weight", "strength", "scale")
    else:
        image_markers = ("controlnet", "control_adapter", "controlnet_processor", "t2i_adapter")
        image_fields = ("image", "image_name", "control_image")
        strength_markers = image_markers
        direct_strength_fields = ("weight", "strength", "control_weight")

    image_matches = 0
    strength_matches = 0
    mask_matches = 0
    matched_nodes: List[str] = []

    for node_id, node_type, inputs, wrapped in _iter_graph_nodes(graph):
        node_changed = False
        if _type_matches(node_type, image_markers):
            changed = _set_existing_input(inputs, image_fields, reference, wrapped=wrapped)
            image_matches += changed
            node_changed = node_changed or bool(changed)

        if _type_matches(node_type, strength_markers):
            changed = _set_existing_input(inputs, direct_strength_fields, float(strength), wrapped=wrapped)
            strength_matches += changed
            node_changed = node_changed or bool(changed)

            # InvokeAI denoise graphs frequently express img2img influence as a
            # start fraction: 0 means full denoise, 1 means no denoise.
            if purpose == PURPOSE_IMAGE_TO_IMAGE:
                changed = _set_existing_input(
                    inputs,
                    ("denoising_start", "start_fraction"),
                    float(1.0 - strength),
                    wrapped=wrapped,
                )
                strength_matches += changed
                node_changed = node_changed or bool(changed)

        if mask_reference is not None and _type_matches(
            node_type,
            ("mask", "denoise_mask", "create_denoise_mask", "inpaint", "infill"),
        ):
            changed = _set_existing_input(
                inputs,
                ("mask", "mask_image", "image", "image_name"),
                mask_reference,
                wrapped=wrapped,
            )
            mask_matches += changed
            node_changed = node_changed or bool(changed)

        if node_changed:
            matched_nodes.append(node_id)

    if image_matches == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The selected InvokeAI graph has no compatible reference-image input for purpose={purpose!r}. "
                "Use a purpose-specific graph containing img2img/image-to-latents, IP-Adapter, or ControlNet nodes."
            ),
        )
    if strength_matches == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The selected InvokeAI graph has no compatible strength/weight input for purpose={purpose!r}; "
                "the requested influence would be ignored."
            ),
        )
    if mask_reference is not None and mask_matches == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "A mask was uploaded, but the selected InvokeAI graph has no compatible mask/inpainting node."
            ),
        )

    return {
        "purpose": purpose,
        "image_inputs_updated": image_matches,
        "strength_inputs_updated": strength_matches,
        "mask_inputs_updated": mask_matches,
        "matched_nodes": matched_nodes,
    }


def _extract_image_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        direct = value.get("image_name")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        image = value.get("image")
        if isinstance(image, dict):
            nested = image.get("image_name")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        for nested_value in value.values():
            found = _extract_image_name(nested_value)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_image_name(item)
            if found:
                return found
    return None


def _multipart_body(
    *,
    fields: Dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> Tuple[bytes, str]:
    boundary = f"----nexus-invokeai-{uuid.uuid4().hex}"
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_filename = (
        os.path.basename(filename or "reference.png")
        .replace("\r", "_")
        .replace("\n", "_")
        .replace('"', "_")
    )
    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{safe_filename}"\r\n'
            ).encode(),
            f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n".encode(),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts), boundary


def _upload_invokeai_image(
    *,
    cfg: shim.ShimConfig,
    filename: str,
    image_bytes: bytes,
    content_type: str,
) -> str:
    if not image_bytes:
        raise HTTPException(status_code=400, detail=f"Uploaded image {filename!r} is empty")
    fields = {
        "image_category": "user",
        "is_intermediate": "false",
        "session_id": "nexus-openai-images-shim",
    }
    body, boundary = _multipart_body(
        fields=fields,
        file_field="file",
        filename=filename,
        content_type=content_type,
        file_bytes=image_bytes,
    )
    candidates = (
        f"{cfg.invokeai_base_url}/api/v1/images/upload",
        (
            f"{cfg.invokeai_base_url}/api/v1/images/upload"
            "?image_category=user&is_intermediate=false&session_id=nexus-openai-images-shim"
        ),
    )
    last_error = ""
    for url in candidates:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            image_name = _extract_image_name(payload)
            if image_name:
                return image_name
            last_error = f"upload response did not contain image_name: {payload}"
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            last_error = f"HTTP {exc.code}: {raw}"
            if exc.code not in {404, 405, 422}:
                break
        except urllib.error.URLError as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            break

    raise HTTPException(status_code=502, detail=f"InvokeAI image upload failed: {last_error or 'unknown error'}")


def _extract_item_id(enqueue_result: Any) -> str:
    if not isinstance(enqueue_result, dict):
        raise HTTPException(status_code=502, detail=f"InvokeAI enqueue returned non-object: {enqueue_result}")
    for key in ("item_ids", "item_id"):
        value = enqueue_result.get(key)
        if isinstance(value, list) and value and isinstance(value[0], (int, str)):
            return str(value[0])
        if isinstance(value, (int, str)):
            return str(value)
    raise HTTPException(status_code=502, detail=f"InvokeAI enqueue returned unexpected payload: {enqueue_result}")


def _enqueue_graph_and_fetch_b64(
    *,
    graph_api: Dict[str, Any],
    output_node_id: str,
    cfg: shim.ShimConfig,
    purpose: str,
) -> str:
    origin = f"openai-images-edit:{int(time.time() * 1000)}"
    enqueue_body = {
        "prepend": True,
        "batch": {
            "graph": graph_api,
            "origin": origin,
            "destination": f"openai-images-edit:{purpose}",
            "runs": 1,
        },
    }

    candidates = shim._discover_queue_enqueue_endpoints(cfg.invokeai_base_url, cfg.queue_id)
    if not candidates:
        candidates = [
            ("POST", f"{cfg.invokeai_base_url}/api/v2/queue/{urllib.parse.quote(cfg.queue_id)}/enqueue_batch"),
            ("POST", f"{cfg.invokeai_base_url}/api/v2/queue/{urllib.parse.quote(cfg.queue_id)}/enqueue"),
            ("POST", f"{cfg.invokeai_base_url}/api/v1/queue/{urllib.parse.quote(cfg.queue_id)}/enqueue_batch"),
            ("POST", f"{cfg.invokeai_base_url}/api/v1/queue/{urllib.parse.quote(cfg.queue_id)}/enqueue"),
        ]

    enqueue_result: Any = None
    last_exc: Optional[HTTPException] = None
    for method, enqueue_url in candidates:
        try:
            enqueue_result = shim._http_json(method, enqueue_url, enqueue_body, timeout=30)
            last_exc = None
            break
        except HTTPException as exc:
            last_exc = exc
            if shim._is_probe_miss(exc):
                continue
            raise
    if last_exc is not None:
        raise last_exc

    item_id = _extract_item_id(enqueue_result)
    queue_id = urllib.parse.quote(cfg.queue_id)
    item = urllib.parse.quote(item_id)
    get_item_urls = (
        f"{cfg.invokeai_base_url}/api/v2/queue/{queue_id}/i/{item}",
        f"{cfg.invokeai_base_url}/api/v2/queue/{queue_id}/items/{item}",
        f"{cfg.invokeai_base_url}/api/v1/queue/{queue_id}/i/{item}",
        f"{cfg.invokeai_base_url}/api/v1/queue/{queue_id}/items/{item}",
    )
    deadline = time.time() + cfg.timeout_s
    last_status: Any = None

    while time.time() < deadline:
        queue_item: Any = None
        last_exc = None
        for get_item_url in get_item_urls:
            try:
                queue_item = shim._http_json("GET", get_item_url, payload=None, timeout=30)
                last_exc = None
                break
            except HTTPException as exc:
                last_exc = exc
                if shim._is_probe_miss(exc):
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        if not isinstance(queue_item, dict):
            raise HTTPException(status_code=502, detail=f"InvokeAI get_queue_item returned non-object: {queue_item}")

        status = queue_item.get("status")
        last_status = status
        if status == "completed":
            image_name = shim._extract_image_name_from_queue_item(queue_item, output_node_id)
            image_urls = (
                f"{cfg.invokeai_base_url}/api/v1/images/i/{urllib.parse.quote(image_name)}/full",
                f"{cfg.invokeai_base_url}/api/v1/images/i/{urllib.parse.quote(image_name)}",
            )
            image_bytes: Optional[bytes] = None
            image_error: Optional[HTTPException] = None
            for image_url in image_urls:
                try:
                    image_bytes = shim._http_bytes(image_url, timeout=60)
                    image_error = None
                    break
                except HTTPException as exc:
                    image_error = exc
                    if shim._is_not_found(exc):
                        continue
                    raise
            if image_bytes is None and image_error is not None:
                raise image_error
            if image_bytes is None:
                raise HTTPException(status_code=502, detail="InvokeAI did not return image bytes")
            if cfg.save_last_image_path:
                shim._best_effort_write_last_image(image_bytes, cfg.save_last_image_path)
            return base64.b64encode(image_bytes).decode("ascii")

        if status == "failed":
            raise HTTPException(
                status_code=502,
                detail=(
                    "InvokeAI edit failed: "
                    f"{queue_item.get('error_type')}: {queue_item.get('error_message')}"
                ),
            )
        if status == "canceled":
            raise HTTPException(status_code=502, detail="InvokeAI edit canceled")
        time.sleep(cfg.poll_interval_s)

    raise HTTPException(
        status_code=504,
        detail=f"Timed out waiting for InvokeAI edit completion (last_status={last_status})",
    )


def _build_edit_graph(
    *,
    spec: EditSpec,
    cfg: shim.ShimConfig,
    image_name: str,
    mask_name: Optional[str],
    seed: Optional[int],
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    template_path = _purpose_template_path(spec.purpose, has_mask=bool(mask_name), cfg=cfg)
    width, height = shim._parse_size(spec.size)
    presets = shim._parse_model_presets(cfg.model_presets_json)
    requested_model = (spec.model or "").strip()
    fallback_model = (cfg.default_model or "").strip()
    requested_or_default = requested_model or fallback_model
    preset = presets.get(requested_or_default.lower()) if requested_or_default else None
    preset_model = str((preset or {}).get("model") or (preset or {}).get("upstream_model") or "").strip()
    model_name = preset_model or requested_or_default

    steps = spec.steps if spec.steps is not None else shim._as_int((preset or {}).get("steps"))
    cfg_scale = spec.cfg_scale if spec.cfg_scale is not None else shim._as_float((preset or {}).get("cfg_scale"))
    scheduler = (spec.scheduler or "").strip() or str((preset or {}).get("scheduler") or "").strip() or None
    model_info = shim._resolve_model_info(model_name, cfg=cfg)

    graph = shim._load_graph_from_template(
        template_path,
        prompt=spec.prompt,
        width=width,
        height=height,
        seed=seed,
        model_info=model_info,
        model_input_mode=cfg.model_input_mode,
        cfg=cfg,
    )
    shim._apply_invokeai_workflow_overrides(
        graph,
        prompt=spec.prompt,
        negative_prompt=spec.negative_prompt,
        width=width,
        height=height,
        seed=seed,
        steps=steps,
        cfg_scale=cfg_scale,
        scheduler=scheduler,
        model_info=model_info,
        model_input_mode=cfg.model_input_mode,
        cfg=cfg,
    )
    diagnostics = inject_reference_inputs(
        graph,
        purpose=spec.purpose,
        image_name=image_name,
        strength=spec.strength,
        mask_name=mask_name,
    )
    output_node_id = (cfg.output_node_id or "").strip() or shim._detect_output_node_id(graph)
    if not output_node_id:
        raise HTTPException(status_code=500, detail="SHIM_OUTPUT_NODE_ID not set and output node could not be detected")

    graph_api = shim._ensure_invokeai_api_graph(graph)
    shim._strip_legacy_board_fields(graph_api)
    diagnostics["template_path"] = template_path
    diagnostics["output_node_id"] = output_node_id
    return graph_api, output_node_id, diagnostics


def _run_edit_batch(
    *,
    spec: EditSpec,
    cfg: shim.ShimConfig,
    image: Tuple[str, bytes, str],
    mask: Optional[Tuple[str, bytes, str]],
) -> Dict[str, Any]:
    image_name = _upload_invokeai_image(
        cfg=cfg,
        filename=image[0],
        image_bytes=image[1],
        content_type=image[2],
    )
    mask_name = None
    if mask is not None:
        mask_name = _upload_invokeai_image(
            cfg=cfg,
            filename=mask[0],
            image_bytes=mask[1],
            content_type=mask[2],
        )

    outputs: List[Dict[str, str]] = []
    diagnostics: Optional[Dict[str, Any]] = None
    for index in range(spec.n):
        seed = spec.seed + index if spec.seed is not None else None
        graph_api, output_node_id, run_diagnostics = _build_edit_graph(
            spec=spec,
            cfg=cfg,
            image_name=image_name,
            mask_name=mask_name,
            seed=seed,
        )
        if diagnostics is None:
            diagnostics = run_diagnostics
        b64_json = _enqueue_graph_and_fetch_b64(
            graph_api=graph_api,
            output_node_id=output_node_id,
            cfg=cfg,
            purpose=spec.purpose,
        )
        outputs.append({"b64_json": b64_json})

    return {
        "created": int(time.time()),
        "data": outputs,
        "_shim": {
            "purpose": spec.purpose,
            "strength": spec.strength,
            "mask": mask is not None,
            "reference_image_name": image_name,
            "mask_image_name": mask_name,
            "workflow": diagnostics or {},
        },
    }


@router.post("/v1/images/edits")
async def images_edits(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    mask: Optional[UploadFile] = File(None),
    purpose: str = Form(PURPOSE_IMAGE_TO_IMAGE),
    strength: float = Form(0.65),
    n: int = Form(1),
    size: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    seed: Optional[int] = Form(None),
    negative_prompt: Optional[str] = Form(None),
    steps: Optional[int] = Form(None),
    cfg_scale: Optional[float] = Form(None),
    guidance_scale: Optional[float] = Form(None),
    scheduler: Optional[str] = Form(None),
) -> Dict[str, Any]:
    normalized_purpose = normalize_purpose(purpose)
    normalized_strength = normalize_strength(strength)
    validate_edit_request(
        purpose=normalized_purpose,
        strength=normalized_strength,
        has_mask=mask is not None,
    )

    if not str(prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string")
    if not 1 <= int(n) <= 8:
        raise HTTPException(status_code=400, detail="n must be between 1 and 8")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="image file is empty")
    if image.content_type and not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="image must have an image/* content type")

    mask_tuple: Optional[Tuple[str, bytes, str]] = None
    if mask is not None:
        mask_bytes = await mask.read()
        if not mask_bytes:
            raise HTTPException(status_code=400, detail="mask file is empty")
        if mask.content_type and not mask.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="mask must have an image/* content type")
        mask_tuple = (
            mask.filename or "mask.png",
            mask_bytes,
            mask.content_type or "application/octet-stream",
        )

    spec = EditSpec(
        prompt=str(prompt).strip(),
        purpose=normalized_purpose,
        strength=normalized_strength,
        n=int(n),
        size=size,
        model=model,
        seed=seed,
        negative_prompt=str(negative_prompt or "").strip(),
        steps=steps,
        cfg_scale=cfg_scale if cfg_scale is not None else guidance_scale,
        scheduler=scheduler,
    )
    cfg = shim._get_config()

    if cfg.mode == "stub":
        return {
            "created": int(time.time()),
            "data": [{"b64_json": shim._STUB_PNG_B64} for _ in range(spec.n)],
            "_shim": {
                "purpose": spec.purpose,
                "strength": spec.strength,
                "mask": mask_tuple is not None,
                "mode": "stub",
            },
        }
    if cfg.mode != "invokeai_queue":
        raise HTTPException(status_code=500, detail=f"Unknown SHIM_MODE '{cfg.mode}'")

    return await asyncio.to_thread(
        _run_edit_batch,
        spec=spec,
        cfg=cfg,
        image=(
            image.filename or "reference.png",
            image_bytes,
            image.content_type or "application/octet-stream",
        ),
        mask=mask_tuple,
    )
