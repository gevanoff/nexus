from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import coding_agent_guarded as guarded_agent
from app import coding_contract_hardening
from app import coding_contract_path_safety
from app import coding_debug_report
from app import coding_evidence_freshness
from app import coding_evidence_policy
from app import coding_execution_dispatch
from app import coding_hypothesis_persistence
from app import coding_inspection_ledger_integrity
from app import coding_model_metadata_resilience
from app import coding_network_resilience
from app import coding_plan_edit_serialization
from app import coding_policy_rejection_recovery
from app import coding_stagnation_resilience
from app import coding_text_tool_handoff
from app import coding_verified_evidence_handoff
from app import coding_routes as routes
from app import coding_workspace as cw


# Install controller/runtime overlays before any Coding Workspace route is invoked.
# Existing route handlers resolve this module-level controller at call time.
coding_network_resilience.install(cw, guarded_agent)
coding_model_metadata_resilience.install(cw.miw)
coding_evidence_policy.install_execution_override_seam(guarded_agent._agent)
coding_text_tool_handoff.install(guarded_agent._agent)
coding_execution_dispatch.install(cw, guarded_agent)
coding_contract_hardening.install(
    guarded_agent._agent,
    coding_evidence_policy,
    coding_debug_report,
)
coding_contract_path_safety.install(coding_contract_hardening)
coding_evidence_freshness.install(coding_evidence_policy)
coding_hypothesis_persistence.install(
    guarded_agent._agent,
    coding_evidence_policy,
    guarded_agent,
)
coding_plan_edit_serialization.install(guarded_agent._agent, guarded_agent)
coding_verified_evidence_handoff.install(
    guarded_agent._agent,
    coding_execution_dispatch,
    coding_hypothesis_persistence,
)
coding_policy_rejection_recovery.install(guarded_agent._agent)
coding_inspection_ledger_integrity.install(coding_stagnation_resilience)
routes.ca = guarded_agent
router = APIRouter()
_DEBUG_SCRIPT_TAG = '<script src="/static/coding_debug_report.js?v=1"></script>'
_CODING_SCRIPT_RE = re.compile(r'<script\s+src="/static/coding\.js(?:\?v=[^"]*)?"\s*></script>')


class CodingFollowUpRequest(BaseModel):
    prompt: str = Field(min_length=1)
    coding_model: Optional[str] = None
    base_branch: Optional[str] = None


def _integrated_reason(task: dict) -> str:
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
    return str(task.get("agent_stop_reason_code") or terminal.get("stop_reason_code") or "").strip()


def _inject_debug_report_script(html: str) -> str:
    if _DEBUG_SCRIPT_TAG in html:
        return html
    match = _CODING_SCRIPT_RE.search(html)
    if match:
        return f"{html[:match.start()]}{_DEBUG_SCRIPT_TAG}\n    {html[match.start():]}"
    return html.replace("</body>", f"  {_DEBUG_SCRIPT_TAG}\n  </body>", 1)


async def _coding_page(req: Request) -> HTMLResponse:
    routes._require_ui_access(req)
    html = Path("app/static/coding.html").read_text(encoding="utf-8")
    return HTMLResponse(
        _inject_debug_report_script(html),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/ui/coding", include_in_schema=False)
async def ui_coding_with_debug_report(req: Request) -> HTMLResponse:
    return await _coding_page(req)


@router.get("/ui/coding/", include_in_schema=False)
async def ui_coding_with_debug_report_slash(req: Request) -> HTMLResponse:
    return await _coding_page(req)


@router.get("/ui/api/coding/tasks/{task_id}/debug-report", include_in_schema=False)
async def ui_coding_debug_report(req: Request, task_id: str) -> PlainTextResponse:
    routes._require_coding_ui(req)
    try:
        active_runner = guarded_agent._active_runner(task_id) is not None
    except Exception:
        active_runner = None
    report = await routes._to_thread(
        coding_debug_report.build_debug_report,
        task_id,
        active_runner=active_runner,
    )
    filename = coding_debug_report.report_filename(task_id)
    return PlainTextResponse(
        report,
        media_type="text/markdown",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


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


# Preserve all existing Coding Workspace routes after the explicit page/report routes.
router.include_router(routes.router)