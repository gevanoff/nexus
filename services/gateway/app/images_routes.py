from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.images_backend import generate_images
from app.backends import get_admission_controller, check_capability
from app.health_checker import check_backend_ready


router = APIRouter()


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
    response_format = body.get("response_format", "url")  # Default to URL

    # Enforce capability and admission control
    # Note: backend selection for images should come from router/config
    # For now, enforce against the configured images backend
    from app.config import S
    
    backend_class = (getattr(S, "IMAGES_BACKEND_CLASS", "") or "").strip() or "gpu_heavy"
    
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

        result = await generate_images(
            prompt=prompt,
            size=str(size),
            n=int(n),
            model=str(model) if isinstance(model, str) and model.strip() else None,
            options=options,
            response_format=response_format,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image backend error: {type(e).__name__}: {e}")
    finally:
        # Release admission slot
        admission.release(backend_class, "images")
