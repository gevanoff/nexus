from __future__ import annotations

import asyncio
from typing import Any

from app import coding_workspace as cw
from app import user_store
from app.agent_api import store
from app.agent_api.auth import AgentAuthContext
from app.agent_api.errors import ApiError
from app.agent_api.schemas import WorkspaceCreate
from app.config import S


async def to_thread(function, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


def git_token(auth: AgentAuthContext) -> str | None:
    try:
        settings = user_store.get_settings(S.USER_DB_PATH, user_id=auth.user_id)
        coding = settings.get("coding") if isinstance(settings, dict) else None
        token = str(coding.get("git_token") or "").strip() if isinstance(coding, dict) else ""
        return token or None
    except Exception:
        return None


async def create_workspace(auth: AgentAuthContext, body: WorkspaceCreate) -> dict[str, Any]:
    if auth.workspace_id:
        raise ApiError(
            403,
            "WORKSPACE_TOKEN_RESTRICTED",
            "A workspace-bound token cannot create another workspace",
        )
    metadata = dict(body.metadata or {})
    created = await to_thread(
        cw.create_task,
        repo_url=str(metadata.get("repo_url") or "").strip() or None,
        base_branch=str(metadata.get("base_branch") or "").strip() or None,
        branch_name=str(metadata.get("branch_name") or "").strip() or None,
        prompt=body.description or body.name,
        owner=str(getattr(auth.user, "username", "") or "agent-api"),
        owner_user_id=auth.user_id,
        git_token_value=git_token(auth),
        coding_model=None,
    )
    workspace_id = str(created.get("id") or "")
    if not workspace_id:
        raise ApiError(500, "WORKSPACE_CREATE_FAILED", "Workspace creation did not return an identifier")
    workspace = await to_thread(
        store.initialize_workspace,
        workspace_id,
        auth,
        name=body.name,
        description=body.description,
        metadata=metadata,
    )
    if workspace.get("status") == "error":
        raise ApiError(
            500,
            "WORKSPACE_INITIALIZATION_FAILED",
            "Workspace repository initialization failed",
            details={"workspace_id": workspace_id},
        )
    return workspace


async def transition_workspace(
    workspace_id: str,
    auth: AgentAuthContext,
    state: str,
) -> dict[str, Any]:
    if state in {"stopped", "archived"}:
        from app import coding_agent as ca

        workspace = await to_thread(store.load_workspace, workspace_id, auth)
        if store.agent_is_active(workspace):
            try:
                await ca.request_pause(workspace_id)
            except Exception:
                pass
    return await to_thread(store.transition_workspace, workspace_id, auth, state)
