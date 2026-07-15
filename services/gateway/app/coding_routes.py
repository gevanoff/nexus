from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import require_bearer
from app.config import S
from app import coding_agent as ca
from app import coding_model_policy
from app import coding_smoke_status
from app import coding_workspace as cw
from app import model_integration_workspace as miw
from app.tools_bus import tool_web_browse
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
    auto_commit: bool = True
    commit_policy: str = "always_on_success"
    push_on_success: bool = False
    draft_pr_on_success: bool = False
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    commit_message: Optional[str] = None
    max_cycles: Optional[int] = Field(default=None, ge=4, le=1000)
    max_runtime_sec: Optional[int] = Field(default=None, ge=60, le=86_400)
    context_reset_cycles: Optional[int] = Field(default=None, ge=0, le=100)


class CodingModelIntegrationCreateRequest(BaseModel):
    model: str
    repo_url: Optional[str] = None
    preferred_runtime: Optional[str] = None
    route_kind: Optional[str] = None
    service_name: Optional[str] = None
    base_branch: Optional[str] = None
    branch_name: Optional[str] = None
    prompt: Optional[str] = None
    coding_model: Optional[str] = None


class CodingModelIntegrationRunRequest(CodingModelIntegrationCreateRequest):
    auto_commit: bool = True
    commit_policy: str = "always_on_success"
    push_on_success: bool = False
    draft_pr_on_success: bool = False
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    commit_message: Optional[str] = None
    max_cycles: Optional[int] = Field(default=None, ge=4, le=1000)
    max_runtime_sec: Optional[int] = Field(default=None, ge=60, le=86_400)
    context_reset_cycles: Optional[int] = Field(default=None, ge=0, le=100)


class CodingAgentRunRequest(BaseModel):
    coding_model: Optional[str] = None
    prompt: Optional[str] = None
    auto_commit: bool = True
    commit_policy: str = "always_on_success"
    push_on_success: bool = False
    draft_pr_on_success: bool = False
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    commit_message: Optional[str] = None
    max_cycles: Optional[int] = Field(default=None, ge=4, le=1000)
    max_runtime_sec: Optional[int] = Field(default=None, ge=60, le=86_400)
    context_reset_cycles: Optional[int] = Field(default=None, ge=0, le=100)


class CodingInterventionRequest(BaseModel):
    action: str
    message: Optional[str] = None
    actor: Optional[str] = None
    coding_model: Optional[str] = None
    auto_commit: bool = True
    commit_message: Optional[str] = None
    max_cycles: Optional[int] = Field(default=None, ge=4, le=1000)
    max_runtime_sec: Optional[int] = Field(default=None, ge=60, le=86_400)
    context_reset_cycles: Optional[int] = Field(default=None, ge=0, le=100)


class CodingGuidanceRequest(BaseModel):
    message: str
    run: bool = False
    coding_model: Optional[str] = None
    auto_commit: bool = True
    commit_message: Optional[str] = None
    max_cycles: Optional[int] = Field(default=None, ge=4, le=1000)
    max_runtime_sec: Optional[int] = Field(default=None, ge=60, le=86_400)
    context_reset_cycles: Optional[int] = Field(default=None, ge=0, le=100)


class CodingProjectPlanRequest(BaseModel):
    goal: Optional[str] = None
    items: Optional[List[Dict[str, Any]]] = None
    note: Optional[str] = None


class CodingTaskModelRequest(BaseModel):
    coding_model: Optional[str] = None


class CodingCommandRequest(BaseModel):
    argv: List[str]
    cwd: Optional[str] = None
    timeout_sec: Optional[float] = None


class CodingSearchRequest(BaseModel):
    query: str
    path: Optional[str] = None
    glob: Optional[str] = None
    fixed_strings: bool = False
    case_sensitive: bool = True
    limit: Optional[int] = 200


class CodingWebBrowseRequest(BaseModel):
    url: str
    max_bytes: Optional[int] = None
    timeout_sec: Optional[float] = None
    extract_links: bool = True
    include_html: bool = False


class CodingFileWriteRequest(BaseModel):
    path: str
    content: str


class CodingTextReplaceRequest(BaseModel):
    path: str
    old_text: str
    new_text: str
    expected_replacements: Optional[int] = 1


class CodingPatchRequest(BaseModel):
    patch: str
    check_only: bool = False


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


def _run_horizon_kwargs(body: Any) -> Dict[str, Optional[int]]:
    return {
        "max_cycles": getattr(body, "max_cycles", None),
        "max_runtime_sec": getattr(body, "max_runtime_sec", None),
        "context_reset_cycles": getattr(body, "context_reset_cycles", None),
    }


def _mission_overrides(body: Any) -> Dict[str, Any]:
    return cw.coding_mission_overrides(
        commit_policy=str(getattr(body, "commit_policy", "always_on_success") or "always_on_success"),
        push_on_success=bool(getattr(body, "push_on_success", False)),
        draft_pr_on_success=bool(getattr(body, "draft_pr_on_success", False)),
        pr_title=str(getattr(body, "pr_title", "") or ""),
        pr_body=str(getattr(body, "pr_body", "") or ""),
        max_cycles=getattr(body, "max_cycles", None),
        max_runtime_sec=getattr(body, "max_runtime_sec", None),
        context_reset_cycles=getattr(body, "context_reset_cycles", None),
    )


def _is_smoke_task(task: Dict[str, Any]) -> bool:
    branch_name = str(task.get("branch_name") or "").strip()
    return branch_name.startswith("nexus-coding-smoke/")


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
    preferred = str(coding.get("model_preference") or coding.get("preferred_model") or "coder").strip() or "coder"
    return coding_model_policy.normalize_preferred_coding_model(preferred)


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


@router.get("/ui/api/coding/tools", include_in_schema=False)
async def ui_coding_tools(req: Request) -> Dict[str, Any]:
    _require_coding_ui(req)
    return ca.coding_tool_manifest()


@router.get("/ui/api/coding/tasks", include_in_schema=False)
async def ui_coding_tasks(req: Request, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    _require_coding_ui(req)
    await ca.recover_stale_agent_runs()
    return {"tasks": await _to_thread(cw.list_tasks, limit)}


@router.post("/ui/api/coding/model-integrations", include_in_schema=False)
async def ui_coding_create_model_integration(req: Request, body: CodingModelIntegrationCreateRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    task = await _to_thread(
        cw.create_model_integration_task,
        model=body.model,
        repo_url=body.repo_url,
        preferred_runtime=body.preferred_runtime,
        route_kind=body.route_kind,
        service_name=body.service_name,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user),
        owner_user_id=_user_id(user),
        git_token_value=_git_token_for_user(user),
        coding_model=_coding_model_for_user(user, body.coding_model),
    )
    return {"task": task}


@router.post("/ui/api/coding/model-integrations/preview", include_in_schema=False)
async def ui_coding_model_integration_preview(req: Request, body: CodingModelIntegrationCreateRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"integration": await _to_thread(miw.build_integration_plan, model=body.model, preferred_runtime=body.preferred_runtime, route_kind=body.route_kind, service_name=body.service_name, prompt=body.prompt)}


@router.post("/ui/api/coding/model-integrations/runs", include_in_schema=False)
async def ui_coding_create_model_integration_and_run(req: Request, body: CodingModelIntegrationRunRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    token = _git_token_for_user(user)
    model = _coding_model_for_user(user, body.coding_model)
    task = await _to_thread(
        cw.create_model_integration_task,
        model=body.model,
        repo_url=body.repo_url,
        preferred_runtime=body.preferred_runtime,
        route_kind=body.route_kind,
        service_name=body.service_name,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user),
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=model,
        mission_overrides=_mission_overrides(body),
    )
    if task.get("status") == "error":
        return {"task": task}
    task = await ca.start_agent_run(
        str(task.get("id") or ""),
        coding_model=model,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user),
        **_run_horizon_kwargs(body),
    )
    return {"task": task}


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
    task = await ca.create_and_start_agent_run(
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user),
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=model,
        commit_message=body.commit_message,
        actor=_actor_from_user(user),
        mission_overrides=_mission_overrides(body),
        **_run_horizon_kwargs(body),
    )
    return {"task": task}


@router.get("/ui/api/coding/tasks/{task_id}", include_in_schema=False)
async def ui_coding_get_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"task": await ca.recover_stale_agent_run(task_id)}


@router.get("/ui/api/coding/tasks/{task_id}/state", include_in_schema=False)
async def ui_coding_get_state(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"state": await _to_thread(cw.coding_state_snapshot, task_id)}


@router.delete("/ui/api/coding/tasks/{task_id}", include_in_schema=False)
async def ui_coding_delete_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.delete_task, task_id)


@router.post("/ui/api/coding/tasks/{task_id}/archive", include_in_schema=False)
async def ui_coding_archive_task(req: Request, task_id: str) -> Dict[str, Any]:
    user = _require_admin(req)
    return await _to_thread(cw.archive_task, task_id, actor=_actor_from_user(user), reason="ui_archive")


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


@router.post("/ui/api/coding/tasks/{task_id}/search", include_in_schema=False)
async def ui_coding_search(req: Request, task_id: str, body: CodingSearchRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(
        cw.search_text,
        task_id,
        query=body.query,
        path=body.path,
        glob=body.glob,
        fixed_strings=body.fixed_strings,
        case_sensitive=body.case_sensitive,
        limit=body.limit or 200,
    )


@router.post("/ui/api/coding/tasks/{task_id}/fetch", include_in_schema=False)
async def ui_coding_fetch_url(req: Request, task_id: str, body: CodingWebBrowseRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    await _to_thread(cw.load_task, task_id)
    return await _to_thread(
        tool_web_browse,
        {
            "url": body.url,
            "max_bytes": body.max_bytes,
            "timeout_sec": body.timeout_sec,
            "extract_links": body.extract_links,
            "include_html": body.include_html,
        },
    )


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
async def ui_coding_read_file(
    req: Request,
    task_id: str,
    path: str = Query(...),
    start_line: Optional[int] = Query(default=None, ge=1),
    line_count: int = Query(default=200, ge=1, le=2000),
) -> Dict[str, Any]:
    _require_coding_ui(req)
    if start_line is not None:
        return await _to_thread(cw.read_file_lines, task_id, path=path, start_line=start_line, line_count=line_count)
    return await _to_thread(cw.read_file, task_id, path=path)


@router.put("/ui/api/coding/tasks/{task_id}/file", include_in_schema=False)
async def ui_coding_write_file(req: Request, task_id: str, body: CodingFileWriteRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.write_file, task_id, path=body.path, content=body.content)


@router.post("/ui/api/coding/tasks/{task_id}/replace", include_in_schema=False)
async def ui_coding_replace_text(req: Request, task_id: str, body: CodingTextReplaceRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(
        cw.replace_text,
        task_id,
        path=body.path,
        old_text=body.old_text,
        new_text=body.new_text,
        expected_replacements=body.expected_replacements,
    )


@router.post("/ui/api/coding/tasks/{task_id}/patch", include_in_schema=False)
async def ui_coding_apply_patch(req: Request, task_id: str, body: CodingPatchRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    return await _to_thread(cw.apply_unified_patch, task_id, patch=body.patch, check_only=body.check_only)


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
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user),
        **_run_horizon_kwargs(body),
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
                auto_commit=body.auto_commit,
                commit_message=body.commit_message,
                actor=_actor_from_user(user),
                **_run_horizon_kwargs(body),
            )
            return {"task": task, "started": True}
        except HTTPException as exc:
            if exc.status_code != 409 or "already running" not in str(exc.detail):
                raise
    task = await _to_thread(cw.append_guidance_message, task_id, message=message, actor=_actor_from_user(user))
    return {"task": task, "started": False}


@router.post("/ui/api/coding/tasks/{task_id}/model", include_in_schema=False)
async def ui_coding_task_model(req: Request, task_id: str, body: CodingTaskModelRequest) -> Dict[str, Any]:
    _require_coding_ui(req)
    task = await _to_thread(cw.set_task_coding_model, task_id, coding_model=body.coding_model)
    return {"task": task}


@router.post("/ui/api/coding/tasks/{task_id}/plan", include_in_schema=False)
async def ui_coding_task_plan(req: Request, task_id: str, body: CodingProjectPlanRequest) -> Dict[str, Any]:
    user = _require_coding_ui(req)
    result = await _to_thread(
        cw.update_project_plan,
        task_id,
        goal=body.goal,
        items=body.items,
        note=body.note,
        actor=_actor_from_user(user),
    )
    stored = await _to_thread(cw.load_task, task_id)
    result["task"] = cw.public_task(stored)
    return result


@router.post("/ui/api/coding/tasks/{task_id}/agent-pause", include_in_schema=False)
async def ui_coding_agent_pause(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_ui(req)
    return {"task": await ca.request_pause(task_id)}


@router.post("/ui/api/coding/tasks/{task_id}/agent-stop", include_in_schema=False)
async def ui_coding_agent_stop(req: Request, task_id: str) -> Dict[str, Any]:
    return await ui_coding_agent_pause(req, task_id)


@router.get("/v1/coding/config")
async def v1_coding_config(req: Request) -> Dict[str, Any]:
    user = _require_coding_api(req)
    token = _git_token_for_user(user) if user is not None else None
    model = _coding_model_for_user(user) if user is not None else ""
    return cw.config_payload(git_token_value=token, preferred_coding_model=model)


@router.get("/v1/coding/tools")
async def v1_coding_tools(req: Request) -> Dict[str, Any]:
    _require_coding_api(req)
    return ca.coding_tool_manifest()


@router.get("/v1/coding/tasks")
async def v1_coding_tasks(req: Request, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    _require_coding_api(req)
    await ca.recover_stale_agent_runs()
    return {"tasks": await _to_thread(cw.list_tasks, limit)}


@router.get("/v1/coding/monitor")
async def v1_coding_monitor(
    req: Request,
    limit: int = Query(default=20, ge=1, le=100),
    only_attention: bool = Query(default=False),
    stalled_after_sec: float = Query(default=900.0, ge=30.0, le=86_400.0),
) -> Dict[str, Any]:
    _require_coding_api(req)
    await ca.recover_stale_agent_runs()
    return await _to_thread(
        cw.monitor_tasks,
        limit=limit,
        only_attention=only_attention,
        stalled_after_sec=stalled_after_sec,
    )


@router.get("/v1/coding/smoke-status")
async def v1_coding_smoke_status(req: Request, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
    _require_coding_api(req)
    return coding_smoke_status.payload(limit=limit)


@router.post("/v1/coding/model-integrations")
async def v1_coding_model_integrations(req: Request, body: CodingModelIntegrationCreateRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    token = _git_token_for_user(user) if user is not None else None
    task = await _to_thread(
        cw.create_model_integration_task,
        model=body.model,
        repo_url=body.repo_url,
        preferred_runtime=body.preferred_runtime,
        route_kind=body.route_kind,
        service_name=body.service_name,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user) if user is not None else "api",
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=_coding_model_for_user(user, body.coding_model) if user is not None else str(body.coding_model or "").strip() or "coder",
    )
    return {"task": task}


@router.post("/v1/coding/model-integrations/preview")
async def v1_coding_model_integration_preview(req: Request, body: CodingModelIntegrationCreateRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"integration": await _to_thread(miw.build_integration_plan, model=body.model, preferred_runtime=body.preferred_runtime, route_kind=body.route_kind, service_name=body.service_name, prompt=body.prompt)}


@router.post("/v1/coding/model-integrations/runs")
async def v1_coding_model_integrations_run(req: Request, body: CodingModelIntegrationRunRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    token = _git_token_for_user(user) if user is not None else None
    model = _coding_model_for_user(user, body.coding_model) if user is not None else str(body.coding_model or "").strip() or "coder"
    task = await _to_thread(
        cw.create_model_integration_task,
        model=body.model,
        repo_url=body.repo_url,
        preferred_runtime=body.preferred_runtime,
        route_kind=body.route_kind,
        service_name=body.service_name,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user) if user is not None else "api",
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=model,
        mission_overrides=_mission_overrides(body),
    )
    if task.get("status") == "error":
        return {"task": task}
    task = await ca.start_agent_run(
        str(task.get("id") or ""),
        coding_model=model,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user) if user is not None else "api",
        **_run_horizon_kwargs(body),
    )
    return {"task": task}


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
    task = await ca.create_and_start_agent_run(
        repo_url=body.repo_url,
        base_branch=body.base_branch,
        branch_name=body.branch_name,
        prompt=body.prompt,
        owner=_actor_from_user(user) if user is not None else "api",
        owner_user_id=_user_id(user),
        git_token_value=token,
        coding_model=model,
        commit_message=body.commit_message,
        actor=_actor_from_user(user) if user is not None else "api",
        mission_overrides=_mission_overrides(body),
        **_run_horizon_kwargs(body),
    )
    return {"task": task}


@router.get("/v1/coding/tasks/{task_id}")
async def v1_coding_get_task(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"task": await ca.recover_stale_agent_run(task_id)}


@router.get("/v1/coding/tasks/{task_id}/inspect")
async def v1_coding_inspect_task(
    req: Request,
    task_id: str,
    stalled_after_sec: float = Query(default=900.0, ge=30.0, le=86_400.0),
) -> Dict[str, Any]:
    _require_coding_api(req)
    await ca.recover_stale_agent_run(task_id)
    return await _to_thread(cw.inspect_task, task_id, stalled_after_sec=stalled_after_sec)


@router.post("/v1/coding/tasks/{task_id}/intervene")
async def v1_coding_intervene(req: Request, task_id: str, body: CodingInterventionRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    action = str(body.action or "").strip().lower()
    if action not in {"resume", "guidance", "guide_and_resume", "pause", "stop"}:
        raise HTTPException(status_code=400, detail="action must be one of resume, guidance, guide_and_resume, pause, stop")

    message = str(body.message or "").strip()
    actor = str(body.actor or "").strip() or (_actor_from_user(user) if user is not None else "coding-api")
    task = await ca.recover_stale_agent_run(task_id)
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    active = str(agent.get("status") or "").strip().lower() in {"queued", "running", "stopping", "pausing"}

    if action == "guidance":
        if not message:
            raise HTTPException(status_code=400, detail="message is required for guidance")
        updated = await _to_thread(cw.append_guidance_message, task_id, message=message, actor=actor)
        return {"ok": True, "action": action, "started": False, "task": updated}

    if action == "pause" or action == "stop":
        updated = await ca.request_pause(task_id)
        return {"ok": True, "action": "pause", "started": False, "task": updated}

    if action == "guide_and_resume":
        if not message:
            raise HTTPException(status_code=400, detail="message is required for guide_and_resume")
        await _to_thread(cw.append_guidance_message, task_id, message=message, actor=actor)
        if active:
            updated = await ca.recover_stale_agent_run(task_id)
            return {"ok": True, "action": action, "started": False, "task": updated}
    elif active:
        raise HTTPException(status_code=409, detail="coding task is already running")

    model = (
        _coding_model_for_user(user, body.coding_model)
        if user is not None
        else str(body.coding_model or task.get("coding_model") or "coder").strip() or "coder"
    )
    updated = await ca.start_agent_run(
        task_id,
        git_token_value=_git_token_for_user(user) if user is not None else None,
        coding_model=model,
        prompt=message or None,
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=actor,
        **_run_horizon_kwargs(body),
    )
    return {"ok": True, "action": action, "started": True, "task": updated}


@router.post("/v1/coding/tasks/{task_id}/archive")
async def v1_coding_archive_task(req: Request, task_id: str) -> Dict[str, Any]:
    user = _require_coding_api(req)
    task = await _to_thread(cw.load_task, task_id)
    actor = _actor_from_user(user) if user is not None else "coding-smoke-harness"
    if user is None:
        if not _is_smoke_task(task):
            raise HTTPException(status_code=403, detail="admin required")
        return await _to_thread(cw.archive_task, task_id, actor=actor, reason="smoke_archive")
    if not bool(getattr(user, "admin", False)):
        raise HTTPException(status_code=403, detail="admin required")
    return await _to_thread(cw.archive_task, task_id, actor=actor, reason="api_archive")


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


@router.post("/v1/coding/tasks/{task_id}/search")
async def v1_coding_search(req: Request, task_id: str, body: CodingSearchRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(
        cw.search_text,
        task_id,
        query=body.query,
        path=body.path,
        glob=body.glob,
        fixed_strings=body.fixed_strings,
        case_sensitive=body.case_sensitive,
        limit=body.limit or 200,
    )


@router.post("/v1/coding/tasks/{task_id}/fetch")
async def v1_coding_fetch_url(req: Request, task_id: str, body: CodingWebBrowseRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    await _to_thread(cw.load_task, task_id)
    return await _to_thread(
        tool_web_browse,
        {
            "url": body.url,
            "max_bytes": body.max_bytes,
            "timeout_sec": body.timeout_sec,
            "extract_links": body.extract_links,
            "include_html": body.include_html,
        },
    )


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
async def v1_coding_read_file(
    req: Request,
    task_id: str,
    path: str = Query(...),
    start_line: Optional[int] = Query(default=None, ge=1),
    line_count: int = Query(default=200, ge=1, le=2000),
) -> Dict[str, Any]:
    _require_coding_api(req)
    if start_line is not None:
        return await _to_thread(cw.read_file_lines, task_id, path=path, start_line=start_line, line_count=line_count)
    return await _to_thread(cw.read_file, task_id, path=path)


@router.put("/v1/coding/tasks/{task_id}/file")
async def v1_coding_write_file(req: Request, task_id: str, body: CodingFileWriteRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.write_file, task_id, path=body.path, content=body.content)


@router.post("/v1/coding/tasks/{task_id}/replace")
async def v1_coding_replace_text(req: Request, task_id: str, body: CodingTextReplaceRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(
        cw.replace_text,
        task_id,
        path=body.path,
        old_text=body.old_text,
        new_text=body.new_text,
        expected_replacements=body.expected_replacements,
    )


@router.post("/v1/coding/tasks/{task_id}/patch")
async def v1_coding_apply_patch(req: Request, task_id: str, body: CodingPatchRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    return await _to_thread(cw.apply_unified_patch, task_id, patch=body.patch, check_only=body.check_only)


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
        auto_commit=body.auto_commit,
        commit_message=body.commit_message,
        actor=_actor_from_user(user) if user is not None else "api",
        **_run_horizon_kwargs(body),
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
                auto_commit=body.auto_commit,
                commit_message=body.commit_message,
                actor=_actor_from_user(user) if user is not None else "api",
                **_run_horizon_kwargs(body),
            )
            return {"task": task, "started": True}
        except HTTPException as exc:
            if exc.status_code != 409 or "already running" not in str(exc.detail):
                raise
    task = await _to_thread(cw.append_guidance_message, task_id, message=message, actor=_actor_from_user(user) if user is not None else "api")
    return {"task": task, "started": False}


@router.post("/v1/coding/tasks/{task_id}/model")
async def v1_coding_task_model(req: Request, task_id: str, body: CodingTaskModelRequest) -> Dict[str, Any]:
    _require_coding_api(req)
    task = await _to_thread(cw.set_task_coding_model, task_id, coding_model=body.coding_model)
    return {"task": task}


@router.post("/v1/coding/tasks/{task_id}/plan")
async def v1_coding_task_plan(req: Request, task_id: str, body: CodingProjectPlanRequest) -> Dict[str, Any]:
    user = _require_coding_api(req)
    result = await _to_thread(
        cw.update_project_plan,
        task_id,
        goal=body.goal,
        items=body.items,
        note=body.note,
        actor=_actor_from_user(user) if user is not None else "api",
    )
    stored = await _to_thread(cw.load_task, task_id)
    result["task"] = cw.public_task(stored)
    return result


@router.post("/v1/coding/tasks/{task_id}/agent-pause")
async def v1_coding_agent_pause(req: Request, task_id: str) -> Dict[str, Any]:
    _require_coding_api(req)
    return {"task": await ca.request_pause(task_id)}


@router.post("/v1/coding/tasks/{task_id}/agent-stop")
async def v1_coding_agent_stop(req: Request, task_id: str) -> Dict[str, Any]:
    return await v1_coding_agent_pause(req, task_id)
