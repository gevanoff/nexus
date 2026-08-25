from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Dict, Mapping


SCHEMA = "nexus_coding_acceptance_epoch.v1"
KEY = "coding_acceptance_epoch"
_REFUTATION_PREFIX = "hypothesis refuted:"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _stdout(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    return str(result.get("stdout") or "").strip()


def _existing_epoch(task: Mapping[str, Any]) -> Dict[str, Any]:
    epoch = _mapping(task.get(KEY))
    if str(epoch.get("schema") or "") != SCHEMA:
        return {}
    if not str(epoch.get("base_head") or "").strip():
        return {}
    return dict(epoch)


def _current_head(cw: Any, task_id: str, task: Mapping[str, Any] | None = None) -> str:
    try:
        result = cw.git_head(task_id)
    except Exception:
        result = {}
    if isinstance(result, Mapping):
        commit = str(result.get("commit") or "").strip()
        if commit:
            return commit
    source = task if isinstance(task, Mapping) else {}
    return str(
        source.get("last_commit")
        or source.get("last_checkpoint_commit")
        or source.get("agent_start_head")
        or ""
    ).strip()


def _has_prior_agent_history(task: Mapping[str, Any]) -> bool:
    runs = task.get("agent_runs") if isinstance(task.get("agent_runs"), list) else []
    if runs:
        return True
    return bool(
        task.get("agent_finished_at")
        or task.get("agent_run_id")
        or task.get("last_checkpoint_run_id")
    )


def _legacy_merge_base(cw: Any, task_id: str, task: Mapping[str, Any], current: str) -> str:
    base_branch = str(task.get("base_branch") or "main").strip() or "main"
    refs = [f"refs/remotes/origin/{base_branch}", f"origin/{base_branch}", base_branch]
    for ref in refs:
        if not current:
            break
        try:
            result = cw.run_task_command(
                task_id,
                argv=["git", "merge-base", current, ref],
                timeout_sec=30,
            )
        except Exception:
            continue
        base = _stdout(result)
        if bool(result.get("ok")) and base:
            return base.splitlines()[0].strip()
    return ""


def _resolve_acceptance_base(
    cw: Any,
    task_id: str,
    task: Mapping[str, Any],
) -> tuple[str, str]:
    current = _current_head(cw, task_id, task)
    if current and not _has_prior_agent_history(task):
        # The authoritative mission baseline is the exact workspace tree before
        # the first agent run. This preserves setup/scaffold commits as baseline
        # rather than incorrectly treating them as agent-produced changes.
        return current, "initial_workspace_head"

    # Migration path for workspaces created before acceptance epochs existed:
    # prior failed/checkpointed runs may already have moved HEAD, so current HEAD
    # cannot safely become accepted baseline truth. Recover the branch fork point.
    legacy = _legacy_merge_base(cw, task_id, task, current)
    if legacy:
        return legacy, "legacy_base_branch_merge_base"
    return str(task.get("agent_start_head") or current).strip(), "legacy_agent_start_head"


def ensure_acceptance_epoch(cw: Any, task_id: str) -> Dict[str, Any]:
    """Initialize the mission epoch for a real persisted workspace before a run."""
    task = cw.load_task(task_id)
    existing = _existing_epoch(task)
    if existing:
        return existing

    base_head, source = _resolve_acceptance_base(cw, task_id, task)
    if not base_head:
        raise RuntimeError("unable to resolve coding mission acceptance base head")
    now = time.time()
    epoch = {
        "schema": SCHEMA,
        "status": "pending",
        "base_head": base_head,
        "accepted_head": "",
        "created_at": now,
        "accepted_at": 0.0,
        "source": source,
    }
    latest = cw.load_task(task_id)
    latest[KEY] = epoch
    cw.save_task(latest)
    return dict(epoch)


def mission_delta_diff(
    cw: Any,
    agent: Any,
    coding_run_delta: Any,
    task_id: str,
    task: Mapping[str, Any] | None = None,
) -> str:
    current_task = dict(task) if isinstance(task, Mapping) else cw.load_task(task_id)
    epoch = _existing_epoch(current_task)
    if not epoch:
        # Legacy/synthetic callers retain the established per-run behavior. Real
        # agent starts initialize the mission epoch before execution.
        return coding_run_delta.run_delta_diff(cw, agent, task_id, current_task)
    base_head = str(epoch.get("base_head") or "").strip()
    run_id = str(current_task.get("agent_run_id") or "mission").strip() or "mission"
    synthetic = dict(current_task)
    synthetic["agent_semantic_baseline"] = {
        "schema": coding_run_delta.SCHEMA,
        "run_id": run_id,
        "tree_commit": base_head,
        "untracked_blobs": {},
        "error": "",
    }
    return coding_run_delta.run_delta_diff(cw, agent, task_id, synthetic)


def mission_acceptance_state(
    cw: Any,
    agent: Any,
    coding_run_delta: Any,
    task_id: str,
    task: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    current_task = dict(task) if isinstance(task, Mapping) else cw.load_task(task_id)
    epoch = _existing_epoch(current_task)
    current_head = _current_head(cw, task_id, current_task)
    if not epoch:
        return {
            "schema": SCHEMA,
            "status": "uninitialized",
            "base_head": "",
            "current_head": current_head,
            "accepted_head": "",
            "has_delta": False,
            "delta_sha256": "",
        }
    diff_text = mission_delta_diff(
        cw,
        agent,
        coding_run_delta,
        task_id,
        current_task,
    )
    return {
        "schema": SCHEMA,
        "status": str(epoch.get("status") or "pending"),
        "base_head": str(epoch.get("base_head") or ""),
        "current_head": current_head,
        "accepted_head": str(epoch.get("accepted_head") or ""),
        "has_delta": bool(str(diff_text or "").strip()),
        "delta_sha256": hashlib.sha256(str(diff_text or "").encode("utf-8")).hexdigest(),
    }


def _worktree_dirty(cw: Any, task_id: str) -> bool:
    try:
        summary = cw.git_change_summary(task_id)
    except Exception:
        return False
    counts = summary.get("counts") if isinstance(summary, Mapping) and isinstance(summary.get("counts"), Mapping) else {}
    return int(counts.get("total") or 0) > 0


def _run_local_delta(cw: Any, task_id: str, task: Mapping[str, Any]) -> bool:
    current_head = _current_head(cw, task_id, task)
    start_head = str(task.get("agent_start_head") or "").strip()
    return bool(start_head and current_head and current_head != start_head) or _worktree_dirty(cw, task_id)


def _mark_epoch_accepted(cw: Any, task_id: str, final_commit: str) -> None:
    latest = cw.load_task(task_id)
    epoch = _existing_epoch(latest)
    if not epoch:
        return
    epoch.update(
        {
            "status": "accepted",
            "accepted_head": str(final_commit or _current_head(cw, task_id, latest)).strip(),
            "accepted_at": time.time(),
        }
    )
    latest[KEY] = epoch
    cw.save_task(latest)


def _refutation_text(args: Mapping[str, Any]) -> str:
    parts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value.strip())
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)

    collect(args)
    return "\n".join(part for part in parts if part)


def _is_explicit_refutation(args: Mapping[str, Any]) -> bool:
    text = _refutation_text(args).strip().casefold()
    return text.startswith(_REFUTATION_PREFIX) and len(text) >= len(_REFUTATION_PREFIX) + 12


def _refutation_available(state: Mapping[str, Any]) -> bool:
    return (
        str(state.get("action_kind") or "") == "edit"
        and state.get("requires_hypothesis") is True
        and bool(state.get("hypothesis_ready"))
    )


def _install_refutation_escape_hatch(forced_action: Any) -> None:
    if bool(getattr(forced_action, "_mission_refutation_escape_installed", False)):
        return

    original_allowed_names = forced_action.allowed_tool_names
    original_evaluate = forced_action.evaluate_tool_call
    original_prompt_context = forced_action.prompt_context

    def allowed_tool_names(task: Mapping[str, Any]) -> set[str]:
        allowed = set(original_allowed_names(task))
        state = forced_action.active_state(task)
        if _refutation_available(state):
            allowed.add("coding_update_plan")
        return allowed

    def evaluate_tool_call(
        task: Mapping[str, Any],
        *,
        name: str,
        args: Mapping[str, Any],
        is_validation_command: Any,
    ) -> tuple[bool, Dict[str, Any]]:
        state = forced_action.active_state(task)
        if _refutation_available(state) and name == "coding_update_plan":
            if _is_explicit_refutation(args):
                return True, {}
            required = str(state.get("required_action") or "").strip()
            return False, {
                "ok": False,
                "error": "forced_action_tool_rejected",
                "state_key": str(state.get("state_key") or ""),
                "required_action": required,
                "message": (
                    "Forced hypothesis-qualified edit mode permits coding_update_plan only as an explicit hypothesis-refutation escape hatch. "
                    "If newly verified evidence contradicts the current hypothesis, rewrite the plan note beginning "
                    "'Hypothesis refuted:' and explain the contradictory repository evidence. Otherwise make the evidence-backed edit or finish with a concrete blocker."
                ),
            }
        return original_evaluate(
            task,
            name=name,
            args=args,
            is_validation_command=is_validation_command,
        )

    def prompt_context(task: Mapping[str, Any]) -> str:
        text = str(original_prompt_context(task) or "")
        state = forced_action.active_state(task)
        if _refutation_available(state):
            text += (
                "\nHypothesis refutation escape hatch: if newly verified repository evidence contradicts the current remediation hypothesis, "
                "call coding_update_plan with a note beginning exactly 'Hypothesis refuted:' and explain what evidence refuted it. "
                "This exceptional tool is exposed only for refutation; ordinary plan churn remains rejected."
            )
        return text

    forced_action.allowed_tool_names = allowed_tool_names
    forced_action.evaluate_tool_call = evaluate_tool_call
    forced_action.prompt_context = prompt_context
    forced_action._mission_refutation_escape_installed = True


def install(
    agent: Any,
    guarded: Any,
    cw: Any,
    coding_run_delta: Any,
    forced_action: Any,
) -> None:
    """Keep semantic acceptance anchored to a workspace mission across resumes."""
    if bool(getattr(guarded, "_mission_acceptance_continuity_installed", False)):
        return

    _install_refutation_escape_hatch(forced_action)

    original_start_agent_run = agent.start_agent_run

    async def start_agent_run_with_epoch(task_id: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        await asyncio.to_thread(ensure_acceptance_epoch, cw, task_id)
        return await original_start_agent_run(task_id, *args, **kwargs)

    agent.start_agent_run = start_agent_run_with_epoch

    def run_delta_diff(task_id: str, task: Mapping[str, Any]) -> str:
        return mission_delta_diff(cw, agent, coding_run_delta, task_id, task)

    guarded._run_delta_diff = run_delta_diff

    original_requires_edits = agent._mission_requires_workspace_edits

    def mission_requires_workspace_edits(task: Dict[str, Any]) -> bool:
        required = bool(original_requires_edits(task))
        if not required:
            return False
        epoch = _existing_epoch(task)
        task_id = str(task.get("id") or "").strip()
        if not epoch or not task_id:
            return required
        if _run_local_delta(cw, task_id, task):
            return True
        state = mission_acceptance_state(cw, agent, coding_run_delta, task_id, task)
        return not bool(state.get("has_delta"))

    agent._mission_requires_workspace_edits = mission_requires_workspace_edits

    original_finalize = agent.finalize_successful_run

    def finalize_successful_run_with_epoch(task_id: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        task = cw.load_task(task_id)
        if not _existing_epoch(task):
            return original_finalize(task_id, *args, **kwargs)
        state = mission_acceptance_state(cw, agent, coding_run_delta, task_id, task)
        original_start_head = str(task.get("agent_start_head") or "")
        substituted = bool(state.get("has_delta")) and not _run_local_delta(cw, task_id, task)
        if substituted:
            task["agent_start_head"] = str(state.get("base_head") or original_start_head)
            cw.save_task(task)
        try:
            result = original_finalize(task_id, *args, **kwargs)
        finally:
            if substituted:
                latest = cw.load_task(task_id)
                latest["agent_start_head"] = original_start_head
                cw.save_task(latest)
        if bool(result.get("ok")):
            _mark_epoch_accepted(cw, task_id, str(result.get("final_commit") or ""))
        return result

    agent.finalize_successful_run = finalize_successful_run_with_epoch

    original_snapshot = cw.coding_state_snapshot

    def snapshot_with_mission_acceptance(task_id: str) -> Dict[str, Any]:
        snapshot = original_snapshot(task_id)
        task = cw.load_task(task_id)
        epoch = _existing_epoch(task)
        if not epoch:
            return snapshot
        state = mission_acceptance_state(cw, agent, coding_run_delta, task_id, task)
        output = dict(snapshot)
        output["mission_acceptance"] = state
        progress = dict(_mapping(snapshot.get("progress")))
        changes = _mapping(snapshot.get("changes"))
        changed_files = changes.get("changed_files") if isinstance(changes.get("changed_files"), list) else []
        validation = _mapping(snapshot.get("validation"))
        diff_review = _mapping(snapshot.get("diff_review"))
        if bool(state.get("has_delta")) and not changed_files:
            if not bool(validation.get("validation_after_latest_edit")):
                progress["current_phase"] = "editing"
                progress["next_recommended_action"] = "validate mission changes"
            elif validation.get("last_validation_ok") is not True:
                progress["current_phase"] = "editing"
                progress["next_recommended_action"] = "resolve failed validation"
            elif not bool(diff_review.get("diff_reviewed_after_latest_edit")):
                progress["current_phase"] = "reviewing"
                progress["next_recommended_action"] = "review mission diff"
            else:
                progress["current_phase"] = "finalizing"
                progress["next_recommended_action"] = "finish the mission"
        elif not bool(state.get("has_delta")) and not changed_files:
            plan = _mapping(task.get("project_plan"))
            items = plan.get("items") if isinstance(plan.get("items"), list) else []
            if not items:
                progress["current_phase"] = "editing"
                progress["next_recommended_action"] = "establish a remediation hypothesis or concrete blocker"
        output["progress"] = progress
        return output

    cw.coding_state_snapshot = snapshot_with_mission_acceptance
    guarded._mission_acceptance_continuity_installed = True
