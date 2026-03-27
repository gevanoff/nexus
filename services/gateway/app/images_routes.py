from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.auth import require_bearer
from app.images_backend import edit_openai_images, generate_images, generate_openai_images, resolve_images_backend_class
from app.backends import backend_provider_name, check_capability, get_admission_controller, get_registry
from app.health_checker import check_backend_ready


router = APIRouter()


def _registry_base_url(backend_class: str) -> str:
    registry = get_registry()
    cfg = registry.get_backend(backend_class)
    if cfg is None:
        raise HTTPException(status_code=400, detail={"error": "backend_not_found", "backend_class": backend_class})
    base_url = (cfg.base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail={"error": "backend_not_ready", "backend_class": backend_class, "message": f"Backend {backend_class} has no base_url configured"},
        )
    return base_url


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
    response_format = body.get("response_format", "url")  # Default to URL

    # Enforce capability and admission control against the selected backend class.
    backend_class = requested_backend_class or resolve_images_backend_class(
        prompt=prompt,
        requested_model=str(model) if isinstance(model, str) and model.strip() else None,
    )
    
    # Check backend health/readiness
    check_backend_ready(backend_class, route_kind="images")
    
    # Check capability
    await check_capability(backend_class, "images")
    
    # Acquire admission slot
    admission = get_admission_controller()
    await admission.acquire(backend_class, "images")
    
    try:
        # Optional quality/tuning knobs (best-effort passthrough; upstream may ignore or reject).
        options = {}
        for k in [
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
            if k in body:
                options[k] = body.get(k)
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")
    finally:
        # Release admission slot
        admission.release(backend_class, "images")


@router.post("/v1/images/edits")
async def images_edits(req: Request):
    require_bearer(req)
    form = await req.form()

    image = form.get("image")
    if not isinstance(image, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="image file is required")

    prompt = form.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string")

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
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="image file is empty")

        mask_field = form.get("mask")
        mask: tuple[str, bytes, str] | None = None
        if isinstance(mask_field, StarletteUploadFile):
            mask_bytes = await mask_field.read()
            if mask_bytes:
                mask = (
                    mask_field.filename or "mask.png",
                    mask_bytes,
                    mask_field.content_type or "application/octet-stream",
                )

        form_fields: dict[str, str] = {}
        for key, value in form.multi_items():
            if key in {"image", "mask", "backend", "backend_class", "response_format"}:
                continue
            if isinstance(value, StarletteUploadFile):
                continue
            if value is None:
                continue
            form_fields[str(key)] = str(value)

        result = await edit_openai_images(
            prompt=prompt,
            image=(
                image.filename or "image.png",
                image_bytes,
                image.content_type or "application/octet-stream",
            ),
            mask=mask,
            form_fields=form_fields,
            response_format=response_format,
            base_url=_registry_base_url(backend_class),
            backend_label=backend_class,
            backend_class=backend_class,
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image edit backend error: {type(e).__name__}: {e}")
    finally:
        admission.release(backend_class, "images")
