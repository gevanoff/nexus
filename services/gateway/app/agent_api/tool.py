from __future__ import annotations

import base64
import binascii
import io
from typing import Any

from fastapi import UploadFile
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.agent_api import service, store
from app.agent_api.auth import AgentToolCaller, authorize_agent_tool
from app.agent_api.constants import OPERATIONS
from app.agent_api.errors import ApiError
from app.agent_api.schemas import ExecuteRequest, TaskCreate, TaskPatch, WorkspaceCreate, WorkspacePatch


_SCOPES = {
    "list_workspaces": "workspaces:read",
    "create_workspace": "workspaces:write",
    "get_workspace": "workspaces:read",
    "update_workspace": "workspaces:write",
    "archive_workspace": "workspaces:write",
    "start_workspace": "workspaces:write",
    "stop_workspace": "workspaces:write",
    "workspace_status": "workspaces:read",
    "list_tasks": "tasks:read",
    "create_task": "tasks:write",
    "get_task": "tasks:read",
    "update_task": "tasks:write",
    "delete_task": "tasks:write",
    "retry_task": "tasks:write",
    "execute": "execute",
    "list_artifacts": "artifacts:read",
    "upload_artifact": "artifacts:write",
    "download_artifact": "artifacts:read",
}


def _required(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ApiError(400, "INVALID_TOOL_ARGUMENTS", f"{name} is required for this operation")
    return normalized


def _limit(parameters: dict[str, Any]) -> int:
    try:
        value = int(parameters.get("limit") or 20)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "INVALID_TOOL_ARGUMENTS", "limit must be an integer") from exc
    if value < 1 or value > 100:
        raise ApiError(400, "INVALID_TOOL_ARGUMENTS", "limit must be between 1 and 100")
    return value


def _validation_error(exc: ValidationError) -> ApiError:
    return ApiError(
        422,
        "VALIDATION_ERROR",
        "Agent API tool input validation failed",
        details={"errors": exc.errors(include_url=False)},
    )


async def _dispatch(
    operation: str,
    workspace_id: str | None,
    task_id: str | None,
    parameters: dict[str, Any],
    caller: AgentToolCaller | None,
) -> Any:
    auth = authorize_agent_tool(caller, _SCOPES.get(operation))
    if operation == "me":
        return {
            "token_type": "personal_access_token" if str(auth.token.get("id") or "") != "ui-session" else "ui_session",
            "scopes": list(auth.scopes),
            "user_id": auth.user_id,
            "workspace_id": auth.workspace_id,
            "rate_limits": auth.rate_limits,
        }
    if operation == "list_workspaces":
        return await service.to_thread(
            store.list_workspaces,
            auth,
            status=str(parameters.get("status") or "").strip() or None,
            limit=_limit(parameters),
            cursor=str(parameters.get("cursor") or "").strip() or None,
        )
    if operation == "create_workspace":
        return await service.create_workspace(auth, WorkspaceCreate(**parameters))

    wid = _required(workspace_id, "workspace_id")
    if operation == "get_workspace":
        return await service.to_thread(store.get_workspace, wid, auth)
    if operation == "update_workspace":
        body = WorkspacePatch(**parameters)
        return await service.to_thread(store.update_workspace, wid, auth, updates=body.model_dump(exclude_unset=True))
    if operation == "archive_workspace":
        return await service.transition_workspace(wid, auth, "archived")
    if operation == "start_workspace":
        return await service.transition_workspace(wid, auth, "running")
    if operation == "stop_workspace":
        return await service.transition_workspace(wid, auth, "stopped")
    if operation == "workspace_status":
        return await service.to_thread(store.workspace_status, wid, auth)
    if operation == "list_tasks":
        return await service.to_thread(
            store.list_tasks,
            wid,
            auth,
            status=str(parameters.get("status") or "").strip() or None,
            limit=_limit(parameters),
            cursor=str(parameters.get("cursor") or "").strip() or None,
        )
    if operation == "create_task":
        body = TaskCreate(**parameters)
        return await service.to_thread(
            store.create_task,
            wid,
            auth,
            instruction=body.instruction,
            context=body.context,
            priority=body.priority,
            max_retries=body.max_retries,
        )

    if operation == "execute":
        body = ExecuteRequest(**parameters)
        return await service.to_thread(
            store.execute,
            wid,
            auth,
            command=body.command,
            code=body.code,
            language=body.language,
            git_token_value=service.git_token(auth),
        )
    if operation == "list_artifacts":
        return await service.to_thread(
            store.list_artifacts,
            wid,
            auth,
            limit=_limit(parameters),
            cursor=str(parameters.get("cursor") or "").strip() or None,
        )
    if operation == "upload_artifact":
        encoded = _required(parameters.get("content_base64"), "parameters.content_base64")
        if len(encoded) > ((store.artifact_max_bytes() + 2) // 3) * 4 + 4:
            raise ApiError(
                413,
                "ARTIFACT_TOO_LARGE",
                "Artifact exceeds the configured upload limit",
                details={"max_bytes": store.artifact_max_bytes()},
            )
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ApiError(400, "INVALID_BASE64", "content_base64 is not valid base64") from exc
        mime_type = str(parameters.get("mime_type") or "application/octet-stream").strip()
        upload = UploadFile(
            file=io.BytesIO(content),
            filename=_required(parameters.get("filename"), "parameters.filename"),
            headers=Headers({"content-type": mime_type}),
        )
        try:
            return await store.save_artifact(
                wid,
                auth,
                upload=upload,
                task_id=str(parameters.get("task_id") or "").strip() or None,
            )
        finally:
            await upload.close()
    if operation == "download_artifact":
        artifact_id = _required(parameters.get("artifact_id"), "parameters.artifact_id")
        artifact, path = await service.to_thread(store.get_artifact, wid, artifact_id, auth)
        max_bytes = min(32_000, max(1, int(parameters.get("max_bytes") or 32_000)))
        if int(artifact.get("size_bytes") or 0) > max_bytes:
            raise ApiError(
                413,
                "ARTIFACT_TOO_LARGE_FOR_TOOL",
                "Artifact is too large for a model tool result; use the REST download endpoint",
                details={"size_bytes": artifact.get("size_bytes"), "max_bytes": max_bytes},
            )
        content = await service.to_thread(path.read_bytes)
        return {"artifact": artifact, "content_base64": base64.b64encode(content).decode("ascii")}

    tid = _required(task_id, "task_id")
    if operation == "get_task":
        return await service.to_thread(store.get_task, wid, tid, auth)
    if operation == "update_task":
        body = TaskPatch(**parameters)
        return await service.to_thread(
            store.update_task,
            wid,
            tid,
            auth,
            updates=body.model_dump(exclude_unset=True),
        )
    if operation == "delete_task":
        await service.to_thread(store.delete_task, wid, tid, auth)
        return {"deleted": True, "task_id": tid}
    if operation == "retry_task":
        return await service.to_thread(store.retry_task, wid, tid, auth)
    raise ApiError(400, "INVALID_OPERATION", f"Unsupported Agent API tool operation: {operation}")


async def execute_agent_api_tool(args: dict[str, Any], caller: AgentToolCaller | None) -> dict[str, Any]:
    operation = str(args.get("operation") or "").strip()
    if operation not in OPERATIONS:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_OPERATION",
                "message": f"operation must be one of: {', '.join(OPERATIONS)}",
                "details": {},
            },
        }
    parameters = args.get("parameters")
    if not isinstance(parameters, dict):
        return {
            "ok": False,
            "error": {"code": "INVALID_TOOL_ARGUMENTS", "message": "parameters must be an object", "details": {}},
        }
    try:
        data = await _dispatch(
            operation,
            str(args.get("workspace_id") or "").strip() or None,
            str(args.get("task_id") or "").strip() or None,
            parameters,
            caller,
        )
        return {"ok": True, "operation": operation, "data": data}
    except ValidationError as exc:
        error = _validation_error(exc)
    except ApiError as exc:
        error = exc
    except (TypeError, ValueError) as exc:
        error = ApiError(400, "INVALID_TOOL_ARGUMENTS", str(exc))
    return {
        "ok": False,
        "operation": operation,
        "error": {"code": error.code, "message": error.message, "details": error.details},
    }
