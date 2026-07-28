from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from app import coding_agent_guarded as guarded_agent
from app import coding_routes as routes
from app import coding_workspace as cw


# Route handlers resolve their module-level controller dependency at call time.
# Bind that dependency to the explicit reconciliation facade before exporting
# the existing router so authentication, request models, and response contracts
# remain unchanged.
routes.ca = guarded_agent
router = routes.router


class CodingFollowUpRequest(BaseModel):
    prompt: str = Field(min_length=1)
    coding_model: Optional[str] = None
    base_branch: Optional[str] = None


def _integrated_reason(task: dict) -> str:
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
    return str(task.get("agent_stop_reason_code") or terminal.get("stop_reason_code") or "").strip()


@router.post("/ui/api/coding/tasks/{task_id}/follow-up", include_in_schema=False)
async def ui_coding_create_follow_up(
    req: Request,
    task_id: str,
    body: CodingFollowUpRequest,
):
    routes._require_coding_ui(req)
    source = await routes._to_thread(cw.load_task, task_id)
    if _integrated_reason(source) != "work_already_integrated":
        raise HTTPException(status_code=409, detail="follow-up workspace is only available after confirmed upstream integration")
    prompt = str(body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="follow-up prompt is required")
    create_body = routes.CodingTaskCreateRequest(
        repo_url=str(source.get("repo_url") or "") or None,
        base_branch=str(body.base_branch or source.get("base_branch") or "main"),
        branch_name=None,
        prompt=prompt,
        coding_model=body.coding_model or source.get("coding_model"),
    )
    result = await routes.ui_coding_create_task(req, create_body)
    return {
        **result,
        "source_task_id": task_id,
        "action": "created_follow_up_workspace",
    }
