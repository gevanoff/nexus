from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import require_bearer
from app.backends import check_capability, get_admission_controller
from app.config import S
from app.health_checker import check_backend_ready
from app.music_backend import generate_music
from app.social_routes import router as social_router
from app.social_publish_route_fixes import router as social_publish_router


router = APIRouter()


@router.post("/v1/music/generations")
async def music_generations(req: Request):
    require_bearer(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    # Minimal validation: require some text input (prompt, lyrics, or style).
    has_input = False
    for field in ["prompt", "lyrics", "style", "input"]:
        val = body.get(field)
        if isinstance(val, str) and val.strip():
            has_input = True
            break
    if not has_input:
        raise HTTPException(status_code=400, detail="must provide prompt, lyrics, style, or input")

    backend_class = (getattr(S, "MUSIC_BACKEND_CLASS", "") or "").strip() or "heartmula_music"

    check_backend_ready(backend_class, route_kind="music")
    await check_capability(backend_class, "music")

    admission = get_admission_controller()
    await admission.acquire(backend_class, "music")
    try:
        return await generate_music(backend_class=backend_class, body=body)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"music backend error: {type(e).__name__}: {e}")
    finally:
        admission.release(backend_class, "music")


# Compose the social drafting and reviewed publishing APIs through the media
# router already imported by app.main.
router.include_router(social_router)
router.include_router(social_publish_router)
