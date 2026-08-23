from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException


def install(agent: Any, guarded: Any, cw: Any, hardening: Any) -> None:
    """Keep guarded dispatch identity while preserving first-consumption provenance.

    The completion-state lifecycle wrapper needs a persisted workspace to record the
    first repository mutation that consumes a remediation hypothesis. Existing
    guarded callers and tests also invoke the semantic tool chain directly with
    synthetic task ids. Route both public aliases through one dispatcher:

    * missing synthetic workspace -> established guarded semantic chain;
    * already-consumed unchanged hypothesis -> established guarded semantic chain,
      preserving the original pre-edit evidence snapshot;
    * otherwise -> completion-state lifecycle wrapper.
    """

    if bool(getattr(agent, "_completion_state_dispatch_installed", False)):
        return

    hardened_run_tool = agent._run_tool
    established_guarded_run_tool = guarded._run_tool_with_semantic_acceptance

    def run_tool_with_completion_state(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Optional[str],
    ) -> Dict[str, Any]:
        try:
            task = cw.load_task(task_id)
        except HTTPException as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 404:
                return established_guarded_run_tool(
                    task_id,
                    name,
                    args,
                    git_token_value=git_token_value,
                )
            raise

        if hardening._matching_consumed_lifecycle(task):
            return established_guarded_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        return hardened_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )

    agent._run_tool = run_tool_with_completion_state
    guarded._run_tool_with_semantic_acceptance = run_tool_with_completion_state
    agent._completion_state_dispatch_installed = True
