from __future__ import annotations

from typing import Any, Dict, Mapping


def install(
    agent: Any,
    evidence_policy: Any,
    range_provenance: Any,
    failed_edit_recovery: Any,
) -> None:
    """Make execution-recovery refinements authoritative after durable-note state.

    ``coding_hypothesis_persistence`` intentionally wraps the provenance facade's
    ``active_state`` and can promote a durable, fresh hypothesis back to edit.
    Range qualification and failed-edit recovery depend on that reconciled state,
    so apply them once more as the final policy layer. Inner installations still
    provide their prompt and execution-time path-lock hooks.
    """
    forced_action = agent.forced_action
    if bool(getattr(forced_action, "_coding_execution_state_finalizer_installed", False)):
        return

    original_active_state = forced_action.active_state

    def active_state_with_execution_recovery(task: Mapping[str, Any]) -> Dict[str, Any]:
        state = original_active_state(task)
        if not state:
            return {}
        ranged = range_provenance.refine_state(
            evidence_policy,
            forced_action,
            task,
            state,
        )
        return failed_edit_recovery.refine_state(agent, task, ranged)

    forced_action.active_state = active_state_with_execution_recovery
    forced_action._active_state_before_execution_state_finalizer = original_active_state
    forced_action._coding_execution_state_finalizer_installed = True
