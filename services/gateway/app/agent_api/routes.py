from __future__ import annotations

import copy
import time
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response

from app.agent_api import service, store
from app.agent_api.auth import AgentAuthContext, validate_api_request
from app.agent_api.errors import AgentApiRoute
from app.agent_api.schemas import ExecuteRequest, TaskCreate, TaskPatch, WorkspaceCreate, WorkspacePatch


router = APIRouter(prefix="/api/v1", tags=["Agent API v1"], route_class=AgentApiRoute)
_STARTED_MONOTONIC = time.monotonic()


def _auth(request: Request, scope: str | None = None) -> AgentAuthContext:
    return validate_api_request(request, scope)


def _as_openapi_30(value: Any) -> Any:
    if isinstance(value, list):
        return [_as_openapi_30(item) for item in value]
    if not isinstance(value, dict):
        return value
    converted = {key: _as_openapi_30(item) for key, item in value.items() if key != "$schema"}
    if "const" in converted:
        converted["enum"] = [converted.pop("const")]
    alternatives = converted.get("anyOf")
    if isinstance(alternatives, list):
        non_null = [item for item in alternatives if not (isinstance(item, dict) and item.get("type") == "null")]
        if len(non_null) != len(alternatives):
            converted["nullable"] = True
            if len(non_null) == 1 and isinstance(non_null[0], dict):
                converted.pop("anyOf", None)
                nullable = converted.pop("nullable")
                if "$ref" in non_null[0]:
                    converted["allOf"] = [non_null[0]]
                else:
                    converted.update(non_null[0])
                converted["nullable"] = nullable
            else:
                converted["anyOf"] = non_null
    return converted


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": str(getattr(request.app, "version", "0.1") or "0.1"),
        "uptime": round(max(0.0, time.monotonic() - _STARTED_MONOTONIC), 3),
    }


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    auth = _auth(request)
    return {
        "token_type": "personal_access_token",
        "token_id": str(auth.token.get("id") or ""),
        "scopes": list(auth.scopes),
        "user_id": auth.user_id,
        "workspace_id": auth.workspace_id,
        "rate_limits": auth.rate_limits,
    }


@router.get("/schema")
async def schema(request: Request) -> dict[str, Any]:
    _auth(request)
    document = _as_openapi_30(copy.deepcopy(request.app.openapi()))
    document["openapi"] = "3.0.3"
    document["info"] = {
        "title": "Nexus Agent API",
        "version": "1.0.0",
        "description": "Agent-first API for Nexus Coding Workspaces.",
    }
    document["paths"] = {
        path: operations
        for path, operations in document.get("paths", {}).items()
        if str(path).startswith("/api/v1/")
    }
    components = document.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {"type": "http", "scheme": "bearer", "bearerFormat": "nxs_pat"}
    for path, operations in document["paths"].items():
        if path == "/api/v1/health":
            continue
        for method, operation in operations.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict):
                operation["security"] = [{"BearerAuth": []}]
    return document


@router.get("/workspaces")
async def list_workspaces(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    auth = _auth(request, "workspaces:read")
    return await service.to_thread(store.list_workspaces, auth, status=status, limit=limit, cursor=cursor)


@router.post("/workspaces")
async def create_workspace(request: Request, body: WorkspaceCreate) -> JSONResponse:
    auth = _auth(request, "workspaces:write")
    workspace = await service.create_workspace(auth, body)
    return JSONResponse(status_code=201, content=jsonable_encoder(workspace))


@router.get("/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    auth = _auth(request, "workspaces:read")
    return await service.to_thread(store.get_workspace, workspace_id, auth)


@router.patch("/workspaces/{workspace_id}")
async def patch_workspace(request: Request, workspace_id: str, body: WorkspacePatch) -> dict[str, Any]:
    auth = _auth(request, "workspaces:write")
    updates = body.model_dump(exclude_unset=True)
    return await service.to_thread(store.update_workspace, workspace_id, auth, updates=updates)


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(request: Request, workspace_id: str) -> Response:
    auth = _auth(request, "workspaces:write")
    await service.transition_workspace(workspace_id, auth, "archived")
    return Response(status_code=204)


@router.post("/workspaces/{workspace_id}/start")
async def start_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    auth = _auth(request, "workspaces:write")
    return await service.transition_workspace(workspace_id, auth, "running")


@router.post("/workspaces/{workspace_id}/stop")
async def stop_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    auth = _auth(request, "workspaces:write")
    return await service.transition_workspace(workspace_id, auth, "stopped")


@router.get("/workspaces/{workspace_id}/status")
async def workspace_status(request: Request, workspace_id: str) -> dict[str, Any]:
    auth = _auth(request, "workspaces:read")
    return await service.to_thread(store.workspace_status, workspace_id, auth)


@router.get("/workspaces/{workspace_id}/tasks")
async def list_tasks(
    request: Request,
    workspace_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    auth = _auth(request, "tasks:read")
    return await service.to_thread(
        store.list_tasks,
        workspace_id,
        auth,
        status=status,
        limit=limit,
        cursor=cursor,
    )


@router.post("/workspaces/{workspace_id}/tasks")
async def create_task(request: Request, workspace_id: str, body: TaskCreate) -> JSONResponse:
    auth = _auth(request, "tasks:write")
    task = await service.to_thread(
        store.create_task,
        workspace_id,
        auth,
        instruction=body.instruction,
        context=body.context,
        priority=body.priority,
        max_retries=body.max_retries,
    )
    return JSONResponse(status_code=201, content=jsonable_encoder(task))


@router.get("/workspaces/{workspace_id}/tasks/{task_id}")
async def get_task(request: Request, workspace_id: str, task_id: str) -> dict[str, Any]:
    auth = _auth(request, "tasks:read")
    return await service.to_thread(store.get_task, workspace_id, task_id, auth)


@router.patch("/workspaces/{workspace_id}/tasks/{task_id}")
async def patch_task(request: Request, workspace_id: str, task_id: str, body: TaskPatch) -> dict[str, Any]:
    auth = _auth(request, "tasks:write")
    return await service.to_thread(
        store.update_task,
        workspace_id,
        task_id,
        auth,
        updates=body.model_dump(exclude_unset=True),
    )


@router.delete("/workspaces/{workspace_id}/tasks/{task_id}", status_code=204)
async def delete_task(request: Request, workspace_id: str, task_id: str) -> Response:
    auth = _auth(request, "tasks:write")
    await service.to_thread(store.delete_task, workspace_id, task_id, auth)
    return Response(status_code=204)


@router.post("/workspaces/{workspace_id}/tasks/{task_id}/retry")
async def retry_task(request: Request, workspace_id: str, task_id: str) -> dict[str, Any]:
    auth = _auth(request, "tasks:write")
    return await service.to_thread(store.retry_task, workspace_id, task_id, auth)


@router.post("/workspaces/{workspace_id}/execute")
async def execute(request: Request, workspace_id: str, body: ExecuteRequest) -> dict[str, Any]:
    auth = _auth(request, "execute")
    return await service.to_thread(
        store.execute,
        workspace_id,
        auth,
        command=body.command,
        code=body.code,
        language=body.language,
        git_token_value=service.git_token(auth),
    )


@router.get("/workspaces/{workspace_id}/artifacts")
async def list_artifacts(
    request: Request,
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    auth = _auth(request, "artifacts:read")
    return await service.to_thread(store.list_artifacts, workspace_id, auth, limit=limit, cursor=cursor)


@router.post("/workspaces/{workspace_id}/artifacts")
async def upload_artifact(
    request: Request,
    workspace_id: str,
    file: UploadFile = File(...),
    task_id: str | None = Form(default=None),
) -> JSONResponse:
    auth = _auth(request, "artifacts:write")
    artifact = await store.save_artifact(workspace_id, auth, upload=file, task_id=task_id)
    return JSONResponse(status_code=201, content=jsonable_encoder(artifact))


@router.get("/workspaces/{workspace_id}/artifacts/{artifact_id}")
async def download_artifact(request: Request, workspace_id: str, artifact_id: str) -> FileResponse:
    auth = _auth(request, "artifacts:read")
    artifact, path = await service.to_thread(store.get_artifact, workspace_id, artifact_id, auth)
    return FileResponse(
        path=str(path),
        filename=str(artifact["filename"]),
        media_type=str(artifact["mime_type"]),
    )
