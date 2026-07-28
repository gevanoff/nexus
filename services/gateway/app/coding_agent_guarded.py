from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from app import coding_agent as _agent
from app import coding_workspace as cw
from app.coding_workspace_reconciliation import reconcile_before_run


async def start_agent_run(
    task_id: str,
    *,
    git_token_value: Optional[str] = None,
    coding_model: Optional[str] = None,
    prompt: Optional[str] = None,
    auto_commit: bool = False,
    commit_message: Optional[str] = None,
    actor: Optional[str] = None,
    max_cycles: Optional[int] = None,
    max_runtime_sec: Optional[int] = None,
    context_reset_cycles: Optional[int] = None,
    mission_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    reconciliation = await reconcile_before_run(
        task_id,
        git_token_value=git_token_value,
        actor=str(actor or "coding-agent"),
    )
    if not reconciliation.get("proceed", True):
        return cw.public_task(reconciliation["task"])
    return await _agent.start_agent_run(
        task_id,
        git_token_value=git_token_value,
        coding_model=coding_model,
        prompt=prompt,
        auto_commit=auto_commit,
        commit_message=commit_message,
        actor=actor,
        max_cycles=max_cycles,
        max_runtime_sec=max_runtime_sec,
        context_reset_cycles=context_reset_cycles,
        mission_overrides=mission_overrides,
    )


async def resume_interrupted_agent_runs(task_ids: Sequence[str]) -> Dict[str, Any]:
    resumable = []
    integrated = []
    failures: Dict[str, str] = {}
    for raw_task_id in task_ids:
        task_id = str(raw_task_id or "").strip()
        if not task_id:
            continue
        try:
            task = await _agent.recover_stale_agent_run(task_id)
            reconciliation = await reconcile_before_run(
                task_id,
                git_token_value=_agent._git_token_for_task_owner(task),
                actor="gateway-recovery",
            )
            if reconciliation.get("proceed", True):
                resumable.append(task_id)
            else:
                integrated.append(task_id)
        except Exception as exc:
            failures[task_id] = f"{type(exc).__name__}: {exc}"
    resumed = await _agent.resume_interrupted_agent_runs(resumable) if resumable else {
        "ok": True,
        "resumed": 0,
        "tasks": [],
        "failures": {},
    }
    combined_failures = dict(resumed.get("failures") or {})
    combined_failures.update(failures)
    return {
        "ok": not combined_failures,
        "resumed": int(resumed.get("resumed") or 0),
        "tasks": list(resumed.get("tasks") or []),
        "integrated": integrated,
        "failures": combined_failures,
    }


def __getattr__(name: str) -> Any:
    return getattr(_agent, name)
