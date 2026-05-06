from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import require_bearer
from app.config import S
from app import coding_agent as ca
from app import coding_workspace as cw
from app import user_store
from app.ui_routes import _require_admin, _require_ui_access, _require_user


router = APIRouter()


class CodingTaskCreateRequest(BaseModel):
    repo_url: Optional[str] = None
    base_branch: Optional[str] = None
    branch_name: Optional[str] = None
    prompt: Optional[str] = None
    coding_model: Optional[str] = None


class CodingCreateAndRunRequest(BaseModel):
    repo_url: Optional[str] = None
    base_branch: Optional[str] = None
    branch_name: Optional[str] = None
    prompt: Optional[str] = None
    coding_model: Optional[str] = None
    max_turns: Optional[int] = None
    max_runtime_sec: Optional[float] = None
    auto_commit: bool = False
    commit_message: Optional[str] = None


class CodingAgentRunRequest(BaseModel):
    coding_model: Optional[str] = None
    prompt: Optional[str] = None
    max_turns: Optional[int] = None
    max_runtime_sec: Optional[float] = None
    auto_commit: bool = False
    commit_message: Optional[str] = None


class CodingGuidanceRequest(BaseModel):
    message: str
    run: bool = False
    coding_model: Optional[str] = None
    max_turns: Optional[int] = None
    max_runtime_sec: Optional[float] = None
    auto_commit: bool = False
    commit_message: Optional[str] = None


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


def _require_coding_api(req: Request):
    if not bool(getattr(S, "CODING_ALLOW_BEARER_API", True)):
        raise HTTPException(status_code=403, detail="coding bearer API is disabled")
    require_bearer(req)
    try:
        return getattr(req.state, "user", None)
    except Exception:
        return None


def _actor_from_user(user: Any) -> str:
    try:
        username = str(getattr(user, "username", "") or "").strip()
        if username:
            return username
    except Exception:
        pass
    return "ui"


def _user_id(user: Any) -> Optional[int]:
    try:
        value = getattr(user, "id", None)
        if value is not None:
            return int(value)
    except Exception:
        return None
    return None


def _settings_for_user(user: Any) -> Dict[str, Any]:
    try:
        if user is not None and getattr(user, "id", None) is not None:
            settings = user_store.get_settings(S.USER_DB_PATH, user_id=int(user.id))
            return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}
    return {}


def _coding_settings_for_user(user: Any) -> Dict[str, Any]:
    settings = _settings_for_user(user)
    coding = settings.get("coding") if isinstance(settings, dict) else None
    return coding if isinstance(coding, dict) else {}


def _git_token_for_user(user: Any) -> str:
    coding = _coding_settings_for_user(user)
    return str(coding.get("git_token") or "").strip()


def _coding_model_for_user(user: Any, requested: Optional[str] = None) -> str:
    explicit = str(requested or "").strip()
    if explicit:
        return explicit
    coding = _coding_settings_for_user(user)
    return str(coding.get("model_preference") or coding.get("preferred_model") or "coder").strip() or "coder"


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
    user = _require_coding_ui(req)
    return cw.config_payload(git_token_value=_git_token_for_user(user), preferred_coding_model=_coding_model_for_user(user))


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
        owner_user_id=_user_id(user),
        git_token_value=_git_token_for_user(user),
        coding_model=_coding_model_for_user(user, body.coding_model),
    )
    return {"task": task}


@router.post("/ui/api/coding/runs", include_in_schema=False)
async def ui_coding_create_and_run(req: Request, body: CodingCreateAndRunRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    token = _git_token_for_user(user)
    model = _coding_model_for_user(user, body.coding_model)
    task = await _to_thread(
        cw.create_task,
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user),
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=model,
    )
    if task.get("status") == "error":
        return {"task": task}
    task = await ca.start_agent_run(
        str(task.get("id") or ""),
        git_token_value=token,
        coding_model=model,
        max_turns=body.max_turns,
        max_runtime_sec=body.max_runtime_sec,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user),
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
    user = _require_coding_ui(req)
    result = await _to_thread(
        cw.run_task_command,
        task_id,
        argv=body.argv,
        cwd=body.cwd,
        timeout_sec=body.timeout_sec,
        git_token_value=_git_token_for_user(user),
    )
    return {"result": result}


@router.get("/ui/api/coding/tasks/{task_id}/status", include_in_schema=False)
async def ui_coding_git_status(req: Request, task_id: str) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    return {"result": await _to_thread(cw.git_status, task_id, git_token_value=_git_token_for_user(user))}


@router.get("/ui/api/coding/tasks/{task_id}/diff", include_in_schema=False)
async def ui_coding_git_diff(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.git_diff, task_id)


@router.get("/ui/api/coding/tasks/{task_id}/changes", include_in_schema=False)
async def ui_coding_git_changes(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"result": await _to_thread(cw.git_change_summary, task_id)}


@router.post("/ui/api/coding/tasks/{task_id}/commit", include_in_schema=False)
async def ui_coding_commit(req: Request, task_id: str, body: CodingCommitRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.commit_task, task_id, message=body.message)


@router.post("/ui/api/coding/tasks/{task_id}/push", include_in_schema=False)
async def ui_coding_push(req: Request, task_id: str, body: CodingPushRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    return {"result": await _to_thread(cw.push_task, task_id, remote=body.remote, git_token_value=_git_token_for_user(user))}


@router.post("/ui/api/coding/tasks/{task_id}/pull-request", include_in_schema=False)
async def ui_coding_pull_request(req: Request, task_id: str, body: CodingPullRequestRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    return {
        "result": await _to_thread(
            cw.create_pull_request,
            task_id,
            title=body.title,
            body=body.body,
            draft=body.draft,
            base_branch=body.base_branch,
            git_token_value=_git_token_for_user(user),
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
    user = _require_coding_ui(req)
    return await _to_thread(cw.agent_brief, task_id, coding_model=_coding_model_for_user(user))


@router.post("/ui/api/coding/tasks/{task_id}/agent-run", include_in_schema=False)
async def ui_coding_agent_run(req: Request, task_id: str, body: CodingAgentRunRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    task = await ca.start_agent_run(
        task_id,
        git_token_value=_git_token_for_user(user),
        coding_model=_coding_model_for_user(user, body.coding_model),
        prompt=body.prompt,
        max_turns=body.max_turns,
        max_runtime_sec=body.max_runtime_sec,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user),
    )
    return {"task": task}


@router.post("/ui/api/coding/tasks/{task_id}/messages", include_in_schema=False)
async def ui_coding_task_message(req: Request, task_id: str, body: CodingGuidanceRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    message = str(body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if body.run:
        try:
            task = await ca.start_agent_run(
                task_id,
                git_token_value=_git_token_for_user(user),
                coding_model=_coding_model_for_user(user, body.coding_model),
                prompt=message,
                max_turns=body.max_turns,
                max_runtime_sec=body.max_runtime_sec,
                auto_commit=body.auto_commit,
                commit_message=body.commit_message,
                actor=_actor_from_user(user),
            )
            return {"task": task, "started": True}
        except HTTPException as exc:
            if exc.status_code != 409 or "already running" not in str(exc.detail):
                raise
    task = await _to_thread(cw.append_guidance_message, task_id, message=message, actor=_actor_from_user(user))
    return {"task": task, "started": False}


@router.post("/ui/api/coding/tasks/{task_id}/agent-stop", include_in_schema=False)
async def ui_coding_agent_stop(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"task": await ca.request_stop(task_id)}


@router.get("/v1/coding/config")
async def v1_coding_config(req: Request) -> Dict[str, Any]:
    user = _require_coding_api(req)
    token = _git_token_for_user(user) if user is not None else None
    model = _coding_model_for_user(user) if user is not None else ""
    return cw.config_payload(git_token_value=token, preferred_coding_model=model)


@router.get("/v1/coding/tasks")
async def v1_coding_tasks(req: Request, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"tasks": await _to_thread(cw.list_tasks, limit)}


@router.post("/v1/coding/tasks")
async def v1_coding_create_task(req: Request, body: CodingTaskCreateRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    token = _git_token_for_user(user) if user is not None else None
    task = await _to_thread(
        cw.create_task,
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user) if user is not None else "api",
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=_coding_model_for_user(user, body.coding_model) if user is not None else str(body.coding_model or "").strip(),
    )
    return {"task": task}


@router.post("/v1/coding/runs")
async def v1_coding_create_and_run(req: Request, body: CodingCreateAndRunRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    token = _git_token_for_user(user) if user is not None else None
    model = _coding_model_for_user(user, body.coding_model) if user is not None else str(body.coding_model or "").strip() or "coder"
    task = await _to_thread(
        cw.create_task,
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user) if user is not None else "api",
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=model,
    )
    if task.get("status") == "error":
        return {"task": task}
    task = await ca.start_agent_run(
        str(task.get("id") or ""),
        git_token_value=token,
        coding_model=model,
        max_turns=body.max_turns,
        max_runtime_sec=body.max_runtime_sec,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user) if user is not None else "api",
    )
    return {"task": task}


@router.get("/v1/coding/tasks/{task_id}")
async def v1_coding_get_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    task = await _to_thread(cw.load_task, task_id)
    return {"task": cw.public_task(task)}


@router.post("/v1/coding/tasks/{task_id}/command")
async def v1_coding_command(req: Request, task_id: str, body: CodingCommandRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    result = await _to_thread(
        cw.run_task_command,
        task_id,
        argv=body.argv,
        cwd=body.cwd,
        timeout_sec=body.timeout_sec,
        git_token_value=_git_token_for_user(user) if user is not None else None,
    )
    return {"result": result}


@router.get("/v1/coding/tasks/{task_id}/status")
async def v1_coding_git_status(req: Request, task_id: str) -> Dict[str, Any]:
    user = _require_coding_api(req)
    return {"result": await _to_thread(cw.git_status, task_id, git_token_value=_git_token_for_user(user) if user is not None else None)}


@router.get("/v1/coding/tasks/{task_id}/diff")
async def v1_coding_git_diff(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.git_diff, task_id)


@router.get("/v1/coding/tasks/{task_id}/changes")
async def v1_coding_git_changes(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"result": await _to_thread(cw.git_change_summary, task_id)}


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
    user = _require_coding_api(req)
    return {"result": await _to_thread(cw.push_task, task_id, remote=body.remote, git_token_value=_git_token_for_user(user) if user is not None else None)}


@router.post("/v1/coding/tasks/{task_id}/pull-request")
async def v1_coding_pull_request(req: Request, task_id: str, body: CodingPullRequestRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    return {
        "result": await _to_thread(
            cw.create_pull_request,
            task_id,
            title=body.title,
            body=body.body,
            draft=body.draft,
            base_branch=body.base_branch,
            git_token_value=_git_token_for_user(user) if user is not None else None,
        )
    }


@router.get("/v1/coding/tasks/{task_id}/agent-brief")
async def v1_coding_agent_brief(req: Request, task_id: str) -> Dict[str, Any]:
    user = _require_coding_api(req)
    model = _coding_model_for_user(user) if user is not None else ""
    return await _to_thread(cw.agent_brief, task_id, coding_model=model)


@router.post("/v1/coding/tasks/{task_id}/agent-run")
async def v1_coding_agent_run(req: Request, task_id: str, body: CodingAgentRunRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    task = await ca.start_agent_run(
        task_id,
        git_token_value=_git_token_for_user(user) if user is not None else None,
        coding_model=_coding_model_for_user(user, body.coding_model) if user is not None else str(body.coding_model or "").strip() or "coder",
        prompt=body.prompt,
        max_turns=body.max_turns,
        max_runtime_sec=body.max_runtime_sec,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user) if user is not None else "api",
    )
    return {"task": task}


@router.post("/v1/coding/tasks/{task_id}/messages")
async def v1_coding_task_message(req: Request, task_id: str, body: CodingGuidanceRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    message = str(body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if body.run:
        try:
            task = await ca.start_agent_run(
                task_id,
                git_token_value=_git_token_for_user(user) if user is not None else None,
                coding_model=_coding_model_for_user(user, body.coding_model) if user is not None else str(body.coding_model or "").strip() or "coder",
                prompt=message,
                max_turns=body.max_turns,
                max_runtime_sec=body.max_runtime_sec,
                auto_commit=body.auto_commit,
                commit_message=body.commit_message,
                actor=_actor_from_user(user) if user is not None else "api",
            )
            return {"task": task, "started": True}
        except HTTPException as exc:
            if exc.status_code != 409 or "already running" not in str(exc.detail):
                raise
    task = await _to_thread(cw.append_guidance_message, task_id, message=message, actor=_actor_from_user(user) if user is not None else "api")
    return {"task": task, "started": False}


@router.post("/v1/coding/tasks/{task_id}/agent-stop")
async def v1_coding_agent_stop(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"task": await ca.request_stop(task_id)}
