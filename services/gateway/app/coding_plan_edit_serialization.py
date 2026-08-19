from __future__ import annotations

import threading
from typing import Any, Dict, Optional


_EDIT_MUTATION_TOOLS = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _task_lock(task_id: str) -> threading.RLock:
    key = str(task_id or "").strip()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def install(agent: Any, guarded_agent: Any = None) -> None:
    """Make plan mutation and forced edit revalidation/mutation one critical section.

    The hypothesis-persistence layer revalidates the durable note immediately
    before edit tools. UI/v1 plan writes use ``coding_workspace.update_project_plan``.
    Serializing both paths with the same per-task RLock removes the remaining
    TOCTOU gap between that revalidation and the actual repository mutation.
    """
    if bool(getattr(agent, "_coding_plan_edit_serialization_installed", False)):
        return

    workspace = agent.cw
    original_update_plan = workspace.update_project_plan

    def update_project_plan_locked(
        task_id: str,
        *,
        goal: Optional[str] = None,
        items: Optional[list[Dict[str, Any]]] = None,
        note: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        with _task_lock(task_id):
            return original_update_plan(
                task_id,
                goal=goal,
                items=items,
                note=note,
                actor=actor,
            )

    workspace.update_project_plan = update_project_plan_locked
    workspace._update_project_plan_before_plan_edit_serialization = original_update_plan

    original_run_tool = agent._run_tool

    def run_tool_with_plan_edit_serialization(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        if str(name or "") not in _EDIT_MUTATION_TOOLS:
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )
        with _task_lock(task_id):
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

    agent._run_tool = run_tool_with_plan_edit_serialization
    if (
        guarded_agent is not None
        and getattr(guarded_agent, "_run_tool_with_semantic_acceptance", None) is original_run_tool
    ):
        guarded_agent._run_tool_with_semantic_acceptance = run_tool_with_plan_edit_serialization
    agent._coding_plan_edit_serialization_installed = True
