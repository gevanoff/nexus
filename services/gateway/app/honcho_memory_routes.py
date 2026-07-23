from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import honcho_memory, user_store
from app.config import S
from app.deployment_admin_routes import router as deployment_admin_router
from app.ui_routes import (
    _require_admin,
    _require_static_bearer_service,
    _require_ui_access,
    _require_user,
)


router = APIRouter()
router.include_router(deployment_admin_router)


async def _body(req: Request) -> Dict[str, Any]:
    try:
        payload = await req.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return payload


@router.get("/v1/telegram/memory/status", include_in_schema=False)
async def telegram_memory_status(req: Request) -> Dict[str, Any]:
    _require_static_bearer_service(req)
    return await honcho_memory.health_status()


@router.post("/v1/telegram/memory/context", include_in_schema=False)
async def telegram_memory_context(req: Request) -> Dict[str, Any]:
    _require_static_bearer_service(req)
    payload = await _body(req)
    try:
        return await honcho_memory.get_context(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"honcho_context_unavailable:{type(exc).__name__}") from exc


@router.post("/v1/telegram/memory/turn", include_in_schema=False)
async def telegram_memory_turn(req: Request) -> Dict[str, Any]:
    _require_static_bearer_service(req)
    payload = await _body(req)
    try:
        return await honcho_memory.ingest_turn(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"honcho_ingest_unavailable:{type(exc).__name__}") from exc


@router.get("/ui/api/user/memory/sessions", include_in_schema=False)
async def user_memory_sessions(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return {"sessions": honcho_memory.list_sessions_for_user(int(user.id))}


@router.delete("/ui/api/user/memory/sessions/{session_id}", include_in_schema=False)
async def user_memory_session_delete(req: Request, session_id: str) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        return await honcho_memory.delete_session_for_user(
            session_id,
            owner_user_id=int(user.id),
            actor_user_id=int(user.id),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="memory session not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"honcho_delete_unavailable:{type(exc).__name__}") from exc


@router.delete("/ui/api/user/memory", include_in_schema=False)
async def user_memory_delete_all(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        return await honcho_memory.delete_all_for_user(owner_user_id=int(user.id), actor_user_id=int(user.id))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"honcho_delete_unavailable:{type(exc).__name__}") from exc


@router.post("/ui/api/admin/memory/exports", include_in_schema=False)
async def admin_memory_export_create(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    admin = _require_admin(req)
    payload = await _body(req)
    try:
        owner_user_id = int(payload.get("user_id"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="numeric user_id is required") from exc
    owner = next((user for user in user_store.list_users(S.USER_DB_PATH) if int(user.id) == owner_user_id and not user.disabled), None)
    if owner is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        result = await honcho_memory.create_export(owner_user_id=owner_user_id, requested_by_user_id=int(admin.id))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"honcho_export_unavailable:{type(exc).__name__}") from exc
    return {"ok": True, "export": result}


@router.get("/ui/api/user/memory/exports", include_in_schema=False)
async def user_memory_exports(req: Request) -> Dict[str, Any]:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return {"exports": honcho_memory.list_exports_for_user(int(user.id))}


@router.get("/ui/api/user/memory/exports/{export_id}/download", include_in_schema=False)
async def user_memory_export_download(req: Request, export_id: str) -> FileResponse:
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        item = honcho_memory.export_for_download(export_id, owner_user_id=int(user.id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="memory export not found") from exc
    path = Path(str(item["path"]))
    return FileResponse(
        str(path),
        media_type="application/json",
        filename=f"nexus-memory-{export_id}.json",
    )
