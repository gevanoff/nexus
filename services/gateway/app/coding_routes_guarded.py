from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import coding_acceptance_convergence_hardening
from app import coding_agent_guarded as guarded_agent
from app import coding_completion_state_dispatch
from app import coding_completion_state_hardening
from app import coding_contract_hardening
from app import coding_contract_path_safety
from app import coding_debug_report
from app import coding_edit_evidence_continuity
from app import coding_evidence_freshness
from app import coding_evidence_policy
from app import coding_evidence_range_provenance
from app import coding_execution_dispatch
from app import coding_execution_state_finalizer
from app import coding_failed_edit_recovery
from app import coding_forced_action
from app import coding_hypothesis_persistence
from app import coding_hypothesis_range_contract
from app import coding_hypothesis_transition_hardening
from app import coding_inspection_ledger_integrity
from app import coding_mission_acceptance_epoch
from app import coding_mission_acceptance_integrity
from app import coding_model_metadata_resilience
from app import coding_network_resilience
from app import coding_plan_edit_serialization
from app import coding_policy_rejection_recovery
from app import coding_refuted_findings
from app import coding_resume_convergence_hardening
from app import coding_semantic_acceptance
from app import coding_semantic_acceptance_contract
from app import coding_stagnation_resilience
from app import coding_terminal_acceptance_hardening
from app import coding_text_tool_handoff
from app import coding_verified_evidence_handoff
from app import coding_work_phases
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
coding_evidence_range_provenance.install(coding_evidence_policy)
coding_failed_edit_recovery.install(
    guarded_agent._agent,
    coding_evidence_policy,
)
coding_hypothesis_persistence.install(
    guarded_agent._agent,
    coding_evidence_policy,
    guarded_agent,
)
coding_execution_state_finalizer.install(
    guarded_agent._agent,
    coding_evidence_policy,
    coding_evidence_range_provenance,
    coding_failed_edit_recovery,
)
coding_hypothesis_range_contract.install(
    guarded_agent._agent,
    coding_evidence_policy,
    coding_evidence_range_provenance,
    coding_hypothesis_persistence,
    guarded_agent,
)
coding_plan_edit_serialization.install(guarded_agent._agent, guarded_agent)
coding_verified_evidence_handoff.install(
    guarded_agent._agent,
    coding_execution_dispatch,
    coding_hypothesis_persistence,
)
coding_edit_evidence_continuity.install(
    guarded_agent._agent,
    coding_execution_dispatch,
    coding_hypothesis_persistence,
    coding_debug_report,
    cw,
)
coding_policy_rejection_recovery.install(guarded_agent._agent)
coding_inspection_ledger_integrity.install(
    coding_stagnation_resilience,
    guarded_agent._agent,
)
# Install lifecycle/transport hardening after all request/tool overlays so it
# observes the final request and established semantic tool chain.
coding_completion_state_hardening.install(
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_execution_dispatch,
    coding_hypothesis_persistence,
    coding_semantic_acceptance,
)
# Durable assistant-derived findings that predate the first consuming mutation
# become auditable superseded history instead of active controller guidance.
coding_refuted_findings.install(
    coding_stagnation_resilience,
    coding_completion_state_hardening,
)
# Preserve the established public guarded-dispatch identity while routing the
# first hypothesis-consuming mutation through completion-state lifecycle logic.
coding_completion_state_dispatch.install(
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_completion_state_hardening,
)
# Close the remaining production acceptance gaps after the established semantic
# dispatch chain is finalized: duplicate semantic-review retries and durable
# validation provenance must both observe the final controller implementation.
coding_terminal_acceptance_hardening.install(
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_work_phases,
)
# A Coding Workspace is one durable mission even when Sentinel or the operator
# starts multiple runner attempts. Checkpoint commits remain inside the pending
# acceptance epoch until the complete branch delta passes semantic acceptance.
coding_mission_acceptance_epoch.install(
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_forced_action,
    coding_terminal_acceptance_hardening,
)
# Bind semantic identity to exact untracked bytes, preserve the exact pre-agent
# baseline for fresh workspaces, and make inherited-only publication fail closed
# if repository state changes after acceptance.
coding_mission_acceptance_integrity.install(
    coding_mission_acceptance_epoch,
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_terminal_acceptance_hardening,
)
# Compose the final task-specific tool schema instead of rebuilding it from raw
# specs. This preserves the four-field hypothesis contract while retaining the
# mission-level refutation tool and turns malformed transition calls into the
# established forced-action rejection/reroute path.
coding_hypothesis_transition_hardening.install(
    guarded_agent._agent,
    cw,
    coding_forced_action,
    coding_hypothesis_persistence,
    coding_evidence_policy,
    coding_debug_report,
)
# The final convergence layer must observe every policy/schema/mission overlay.
# It executes refutation against the live policy that advertised the tool and
# forces reviewed+validated mission deltas through independent semantic
# acceptance before the agent can resume broad inspection.
coding_acceptance_convergence_hardening.install(
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_mission_acceptance_epoch,
    coding_semantic_acceptance,
)
# Runner attempts and Sentinel supervision must not erase mission-scoped output
# obligations. Derive validate -> review -> finish from durable mission state on
# every resume, and leave generic failed runs stable for human attention instead
# of treating them as automatically recoverable infrastructure interruptions.
coding_resume_convergence_hardening.install(
    guarded_agent._agent,
    cw,
    coding_mission_acceptance_epoch,
    coding_acceptance_convergence_hardening,
)
# Semantic acceptance is the final authority before publication. Freeze human
# mission intent separately from the agent-authored plan, ground the reviewer in
# surrounding repository code, and preserve its decision rationale in debug
# reports. Install last so no earlier dispatch overlay can bypass this contract.
coding_semantic_acceptance_contract.install(
    guarded_agent._agent,
    guarded_agent,
    cw,
    coding_mission_acceptance_epoch,
    coding_terminal_acceptance_hardening,
    coding_semantic_acceptance,
    coding_debug_report,
)

routes.ca = guarded_agent
router = APIRouter()
_DEBUG_SCRIPT_TAG = '<script src="/static/coding_debug_report.js?v=1"></script>'
_TERMINAL_WATCH_SCRIPT_TAG = '<script src="/static/coding_terminal_status_watch.js?v=1"></script>'
_CODING_SCRIPT_RE = re.compile(r'<script\s+src="/static/coding\.js(?:\?v=[^"]*)?"\s*></script>')


class CodingFollowUpRequest(BaseModel):
    prompt: str = Field(min_length=1)
    coding_model: Optional[str] = None
    base_branch: Optional[str] = None


class CodingAcceptanceContractRequest(BaseModel):
    acceptance_criteria: List[str] = Field(min_length=1, max_length=40)


def _integrated_reason(task: dict) -> str:
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
    return str(task.get("agent_stop_reason_code") or terminal.get("stop_reason_code") or "").strip()


def _inject_debug_report_script(html: str) -> str:
    missing = [
        tag
        for tag in (_DEBUG_SCRIPT_TAG, _TERMINAL_WATCH_SCRIPT_TAG)
        if tag not in html
    ]
    if not missing:
        return html
    injected = "\n    ".join(missing)
    match = _CODING_SCRIPT_RE.search(html)
    if match:
        return f"{html[:match.start()]}{injected}\n    {html[match.start():]}"
    return html.replace("</body>", f"  {injected}\n  </body>", 1)


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


@router.put("/ui/api/coding/tasks/{task_id}/acceptance-contract", include_in_schema=False)
async def ui_coding_set_acceptance_contract(
    req: Request,
    task_id: str,
    body: CodingAcceptanceContractRequest,
):
    routes._require_coding_ui(req)
    try:
        contract = await routes._to_thread(
            coding_semantic_acceptance_contract.set_acceptance_criteria,
            cw,
            task_id,
            body.acceptance_criteria,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"acceptance_contract": contract}


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
