from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import FormData, UploadFile as StarletteUploadFile

from app.auth import require_bearer
from app.images_backend import edit_openai_images, generate_images, generate_openai_images, resolve_images_backend_class
from app.backends import backend_provider_name, check_capability, get_admission_controller, get_registry
from app.health_checker import check_backend_ready


router = APIRouter()

_EDIT_PURPOSES = {"image_to_image", "composition", "style", "controlnet"}
_EDIT_PURPOSE_ALIASES = {
    "img2img": "image_to_image",
    "image-to-image": "image_to_image",
    "image_to_image": "image_to_image",
    "composition": "composition",
    "composition_reference": "composition",
    "style": "style",
    "style_reference": "style",
    "controlnet": "controlnet",
    "control_net": "controlnet",
}


def _registry_base_url(backend_class: str) -> str:
    registry = get_registry()
    cfg = registry.get_backend(backend_class)
    if cfg is None:
        raise HTTPException(status_code=400, detail={"error": "backend_not_found", "backend_class": backend_class})
    base_url = (cfg.base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "backend_not_ready",
                "backend_class": backend_class,
                "message": f"Backend {backend_class} has no base_url configured",
            },
        )
    return base_url


def _normalize_edit_purpose(value: Any) -> str:
    raw = str(value or "image_to_image").strip().lower().replace(" ", "_")
    purpose = _EDIT_PURPOSE_ALIASES.get(raw, raw)
    if purpose not in _EDIT_PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be one of: {', '.join(sorted(_EDIT_PURPOSES))}",
        )
    return purpose


def _normalize_edit_strength(value: Any) -> float:
    try:
        strength = float(0.65 if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="strength must be a number between 0 and 1") from exc
    if not 0.0 <= strength <= 1.0:
        raise HTTPException(status_code=400, detail="strength must be between 0 and 1")
    return strength


def _normalize_edit_count(value: Any) -> int:
    try:
        count = int(1 if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="n must be an integer >= 1") from exc
    if count < 1:
        raise HTTPException(status_code=400, detail="n must be >= 1")
    return min(count, 8)


async def _upload_tuple(
    upload: StarletteUploadFile,
    *,
    field_name: str,
    default_filename: str,
) -> tuple[str, bytes, str]:
    content_type = str(upload.content_type or "").strip()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"{field_name} must have an image/* content type")
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail=f"{field_name} file is empty")
    return (
        upload.filename or default_filename,
        raw,
        content_type or "application/octet-stream",
    )


def _edit_form_fields(
    form: FormData,
    *,
    purpose: str,
    strength: float,
    count: int,
) -> dict[str, str]:
    fields: dict[str, str] = {
        "purpose": purpose,
        "strength": str(strength),
        "n": str(count),
    }
    for key, value in form.multi_items():
        if key in {
            "image",
            "mask",
            "prompt",
            "backend",
            "backend_class",
            "response_format",
            "purpose",
            "strength",
            "n",
        }:
            continue
        if isinstance(value, StarletteUploadFile) or value is None:
            continue
        fields[str(key)] = str(value)
    return fields


async def _execute_images_edit(form: FormData) -> dict[str, Any]:
    image = form.get("image")
    if not isinstance(image, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="image file is required")

    prompt = form.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string")

    purpose = _normalize_edit_purpose(form.get("purpose"))
    strength = _normalize_edit_strength(form.get("strength"))
    count = _normalize_edit_count(form.get("n"))

    mask_field = form.get("mask")
    if mask_field is not None and not isinstance(mask_field, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="mask must be an uploaded image")
    if isinstance(mask_field, StarletteUploadFile) and purpose != "image_to_image":
        raise HTTPException(
            status_code=400,
            detail="mask upload is only supported for image_to_image/inpainting workflows",
        )

    model = form.get("model")
    requested_backend_class = str(form.get("backend_class") or form.get("backend") or "").strip()
    backend_class = requested_backend_class or resolve_images_backend_class(
        prompt=prompt,
        requested_model=str(model) if isinstance(model, str) and model.strip() else None,
    )
    response_format = str(form.get("response_format") or "url").strip().lower()
    if response_format not in {"url", "b64_json"}:
        raise HTTPException(status_code=400, detail="response_format must be 'url' or 'b64_json'")

    check_backend_ready(backend_class, route_kind="images")
    await check_capability(backend_class, "images")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "images")
    try:
        image_tuple = await _upload_tuple(
            image,
            field_name="image",
            default_filename="reference.png",
        )
        mask_tuple: tuple[str, bytes, str] | None = None
        if isinstance(mask_field, StarletteUploadFile):
            mask_tuple = await _upload_tuple(
                mask_field,
                field_name="mask",
                default_filename="mask.png",
            )

        form_fields = _edit_form_fields(
            form,
            purpose=purpose,
            strength=strength,
            count=count,
        )
        return await edit_openai_images(
            prompt=prompt.strip(),
            image=image_tuple,
            mask=mask_tuple,
            form_fields=form_fields,
            response_format=response_format,
            base_url=_registry_base_url(backend_class),
            backend_label=backend_class,
            backend_class=backend_class,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"image edit backend error: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        admission.release(backend_class, "images")


@router.post("/v1/images/generations")
async def images_generations(req: Request):
    require_bearer(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string")

    n = body.get("n", 1)
    size = body.get("size", "1024x1024")
    model = body.get("model")
    requested_backend_class = str(body.get("backend_class") or body.get("backend") or "").strip()
    response_format = body.get("response_format", "url")

    backend_class = requested_backend_class or resolve_images_backend_class(
        prompt=prompt,
        requested_model=str(model) if isinstance(model, str) and model.strip() else None,
    )

    check_backend_ready(backend_class, route_kind="images")
    await check_capability(backend_class, "images")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "images")

    try:
        options = {}
        for key in [
            "seed",
            "steps",
            "num_inference_steps",
            "guidance",
            "guidance_scale",
            "cfg_scale",
            "negative_prompt",
            "sampler",
            "scheduler",
            "style",
            "quality",
        ]:
            if key in body:
                options[key] = body.get(key)
        if not options:
            options = None

        model_name = str(model) if isinstance(model, str) and model.strip() else None
        if backend_provider_name(backend_class) == "mlx":
            result = await generate_openai_images(
                prompt=prompt,
                size=str(size),
                n=int(n),
                model=model_name,
                options=options,
                response_format=response_format,
                base_url=_registry_base_url(backend_class),
                backend_label=backend_class,
                backend_class=backend_class,
            )
        else:
            result = await generate_images(
                prompt=prompt,
                size=str(size),
                n=int(n),
                model=model_name,
                options=options,
                response_format=response_format,
                backend_class=backend_class,
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(exc).__name__}: {exc}") from exc
    finally:
        admission.release(backend_class, "images")


@router.post("/v1/images/edits")
async def images_edits(req: Request):
    require_bearer(req)
    return await _execute_images_edit(await req.form())


@router.post("/ui/api/image/edit", include_in_schema=False)
async def ui_images_edit(req: Request):
    # Import lazily because main imports ui_routes before images_routes.
    from app.ui_routes import _require_ui_access, _require_user

    _require_ui_access(req)
    _require_user(req)
    return await _execute_images_edit(await req.form())
