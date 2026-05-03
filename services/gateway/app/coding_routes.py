from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import require_bearer
from app.config import S
from app import coding_workspace as cw
from app.ui_routes import _require_admin, _require_ui_access, _require_user


router = APIRouter()


class CodingTaskCreateRequest(BaseModel):
    repo_url: Optional[str] = None
    base_branch: Optional[str] = None
    branch_name: Optional[str] = None
    prompt: Optional[str] = None


class CodingCommandRequest(BaseModel):
    argv: List[str]
    cwd: Optional[str] = None
    timeout_sec: Optional[float] = None


class CodingFileWriteRequest(BaseModel):
    path: str
    content: str


class CodingCommitRequest(BaseModel):
    message: str


class CodingPushRequest(BaseModel):
    remote: Optional[str] = "origin"


class CodingPullRequestRequest(BaseModel):
    title: str
    body: Optional[str] = ""
    draft: bool = True
    base_branch: Optional[str] = None


def _require_coding_ui(req: Request):
    _require_ui_access(req)
    if not bool(getattr(S, "USER_AUTH_ENABLED", True)):
        return None
    if bool(getattr(S, "CODING_REQUIRE_ADMIN", True)):
        return _require_admin(req)
    return _require_user(req)


def _require_coding_api(req: Request) -> None:
    if not bool(getattr(S, "CODING_ALLOW_BEARER_API", True)):
        raise HTTPException(status_code=403, detail="coding bearer API is disabled")
    require_bearer(req)


def _actor_from_user(user: Any) -> str:
    try:
        username = str(getattr(user, "username", "") or "").strip()
        if username:
            return username
    except Exception:
        pass
    return "ui"


async def _to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@router.get("/ui/coding", include_in_schema=False)
async def ui_coding(req: Request):
    _require_ui_access(req)
    return FileResponse("app/static/coding.html")


@router.get("/ui/coding/", include_in_schema=False)
async def ui_coding_slash(req: Request):
    return await ui_coding(req)


@router.get("/ui/api/coding/config", include_in_schema=False)
async def ui_coding_config(req: Request) -> Dict[str, Any]:
    _require_coding_ui(req)
    return cw.config_payload()


@router.get("/ui/api/coding/tasks", include_in_schema=False)
async def ui_coding_tasks(req: Request, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"tasks": await _to_thread(cw.list_tasks, limit)}


@router.post("/ui/api/coding/tasks", include_in_schema=False)
async def ui_coding_create_task(req: Request, body: CodingTaskCreateRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    task = await _to_thread(
        cw.create_task,
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user),
    )
    return {"task": task}


@router.get("/ui/api/coding/tasks/{task_id}", include_in_schema=False)
async def ui_coding_get_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    task = await _to_thread(cw.load_task, task_id)
    return {"task": cw.public_task(task)}


@router.delete("/ui/api/coding/tasks/{task_id}", include_in_schema=False)
async def ui_coding_delete_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.delete_task, task_id)


@router.post("/ui/api/coding/tasks/{task_id}/command", include_in_schema=False)
async def ui_coding_command(req: Request, task_id: str, body: CodingCommandRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    result = await _to_thread(cw.run_task_command, task_id, argv=body.argv, cwd=body.cwd, timeout_sec=body.timeout_sec)
    return {"result": result}


@router.get("/ui/api/coding/tasks/{task_id}/status", include_in_schema=False)
async def ui_coding_git_status(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"result": await _to_thread(cw.git_status, task_id)}


@router.get("/ui/api/coding/tasks/{task_id}/diff", include_in_schema=False)
async def ui_coding_git_diff(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.git_diff, task_id)


@router.post("/ui/api/coding/tasks/{task_id}/commit", include_in_schema=False)
async def ui_coding_commit(req: Request, task_id: str, body: CodingCommitRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.commit_task, task_id, message=body.message)


@router.post("/ui/api/coding/tasks/{task_id}/push", include_in_schema=False)
async def ui_coding_push(req: Request, task_id: str, body: CodingPushRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"result": await _to_thread(cw.push_task, task_id, remote=body.remote)}


@router.post("/ui/api/coding/tasks/{task_id}/pull-request", include_in_schema=False)
async def ui_coding_pull_request(req: Request, task_id: str, body: CodingPullRequestRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {
        "result": await _to_thread(
            cw.create_pull_request,
            task_id,
            title=body.title,
            body=body.body,
            draft=body.draft,
            base_branch=body.base_branch,
        )
    }


@router.get("/ui/api/coding/tasks/{task_id}/tree", include_in_schema=False)
async def ui_coding_tree(
    req: Request,
    task_id: str,
    path: str = Query(default=""),
    limit: int = Query(default=250, ge=1, le=1000),
) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.list_tree, task_id, path=path, limit=limit)


@router.get("/ui/api/coding/tasks/{task_id}/file", include_in_schema=False)
async def ui_coding_read_file(req: Request, task_id: str, path: str = Query(...)) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.read_file, task_id, path=path)


@router.put("/ui/api/coding/tasks/{task_id}/file", include_in_schema=False)
async def ui_coding_write_file(req: Request, task_id: str, body: CodingFileWriteRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.write_file, task_id, path=body.path, content=body.content)


@router.get("/ui/api/coding/tasks/{task_id}/agent-brief", include_in_schema=False)
async def ui_coding_agent_brief(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.agent_brief, task_id)


@router.get("/v1/coding/config")
async def v1_coding_config(req: Request) -> Dict[str, Any]:
    _require_coding_api(req)
    return cw.config_payload()


@router.get("/v1/coding/tasks")
async def v1_coding_tasks(req: Request, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"tasks": await _to_thread(cw.list_tasks, limit)}


@router.post("/v1/coding/tasks")
async def v1_coding_create_task(req: Request, body: CodingTaskCreateRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    task = await _to_thread(
        cw.create_task,
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner="api",
    )
    return {"task": task}


@router.get("/v1/coding/tasks/{task_id}")
async def v1_coding_get_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    task = await _to_thread(cw.load_task, task_id)
    return {"task": cw.public_task(task)}


@router.post("/v1/coding/tasks/{task_id}/command")
async def v1_coding_command(req: Request, task_id: str, body: CodingCommandRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    result = await _to_thread(cw.run_task_command, task_id, argv=body.argv, cwd=body.cwd, timeout_sec=body.timeout_sec)
    return {"result": result}


@router.get("/v1/coding/tasks/{task_id}/status")
async def v1_coding_git_status(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"result": await _to_thread(cw.git_status, task_id)}


@router.get("/v1/coding/tasks/{task_id}/diff")
async def v1_coding_git_diff(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.git_diff, task_id)


@router.get("/v1/coding/tasks/{task_id}/tree")
async def v1_coding_tree(
    req: Request,
    task_id: str,
    path: str = Query(default=""),
    limit: int = Query(default=250, ge=1, le=1000),
) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.list_tree, task_id, path=path, limit=limit)


@router.get("/v1/coding/tasks/{task_id}/file")
async def v1_coding_read_file(req: Request, task_id: str, path: str = Query(...)) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.read_file, task_id, path=path)


@router.put("/v1/coding/tasks/{task_id}/file")
async def v1_coding_write_file(req: Request, task_id: str, body: CodingFileWriteRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.write_file, task_id, path=body.path, content=body.content)


@router.post("/v1/coding/tasks/{task_id}/commit")
async def v1_coding_commit(req: Request, task_id: str, body: CodingCommitRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.commit_task, task_id, message=body.message)


@router.post("/v1/coding/tasks/{task_id}/push")
async def v1_coding_push(req: Request, task_id: str, body: CodingPushRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"result": await _to_thread(cw.push_task, task_id, remote=body.remote)}


@router.post("/v1/coding/tasks/{task_id}/pull-request")
async def v1_coding_pull_request(req: Request, task_id: str, body: CodingPullRequestRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return {
        "result": await _to_thread(
            cw.create_pull_request,
            task_id,
            title=body.title,
            body=body.body,
            draft=body.draft,
            base_branch=body.base_branch,
        )
    }


@router.get("/v1/coding/tasks/{task_id}/agent-brief")
async def v1_coding_agent_brief(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.agent_brief, task_id)
