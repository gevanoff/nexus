from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Sequence

from app import coding_stagnation_resilience as resilience


SCHEMA = "nexus_coding_forced_action.v1"
_BASE_ALLOWED_TOOLS = {
    "coding_write_file",
    "coding_replace_text",
    "coding_apply_patch",
    "coding_run_command",
    "coding_git_diff",
    "coding_finish",
}
_ACTION_ALLOWED_TOOLS = {
    "edit": {
        "coding_write_file",
        "coding_replace_text",
        "coding_apply_patch",
        "coding_finish",
    },
    "validate": {"coding_run_command", "coding_finish"},
    "diff_review": {"coding_git_diff", "coding_finish"},
    "finish": {"coding_finish"},
    "bounded": set(_BASE_ALLOWED_TOOLS),
}


def _raw_active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    raw = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    if str(raw.get("schema") or "") != SCHEMA or str(raw.get("status") or "") != "active":
        return {}
    if str(raw.get("state_key") or "") != resilience.durable_state_key(task):
        return {}
    return dict(raw)


def _event_timestamp(event: Mapping[str, Any]) -> float:
    try:
        return float(event.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _attempt_count(task: Mapping[str, Any], state: Mapping[str, Any]) -> int:
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    try:
        activated_at = float(state.get("activated_at") or 0)
    except (TypeError, ValueError):
        activated_at = 0.0
    legacy_start = max(0, min(int(state.get("activation_event_count") or 0), len(events)))
    count = 0
    for index, event in enumerate(events):
        event_ts = _event_timestamp(event)
        if activated_at > 0 and event_ts > 0:
            # Persisted event timestamps remain stable when the capped event
            # list rolls over and its list indices shift.
            if event_ts < activated_at:
                continue
        elif index < legacy_start:
            # Compatibility fallback for old or synthetic events without
            # timestamps. If rollover makes the index ambiguous, undercounting
            # is safer than prematurely exhausting the forced-action attempt.
            continue
        if str(event.get("type") or "") != "tool_finished":
            continue
        name = str(event.get("name") or "").strip()
        if name == "coding_finish" or name not in _BASE_ALLOWED_TOOLS:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if str(result.get("error") or "") == "forced_action_tool_rejected":
            continue
        count += 1
    return count


def _effective_allowed_tools(state: Mapping[str, Any], attempt_count: int) -> set[str]:
    # Forced-action mode is a tool-class restriction, not a single-tool lease.
    # Keep the required action class available until the durable state changes
    # or normal controller escalation terminates the run. Collapsing to
    # coding_finish after one tool call can deadlock unchanged resumes: the
    # workspace still requires an edit, while the only remaining action is a
    # finish call that the no-change audit must reject.
    _ = attempt_count
    kind = str(state.get("action_kind") or "bounded")
    return set(_ACTION_ALLOWED_TOOLS.get(kind, _ACTION_ALLOWED_TOOLS["bounded"]))


def active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    state = _raw_active_state(task)
    if not state:
        return {}
    attempts = _attempt_count(task, state)
    state["attempt_count"] = attempts
    state["allowed_tools"] = sorted(_effective_allowed_tools(state, attempts))
    return state


def activate(
    task: Mapping[str, Any],
    *,
    state_key: str,
    run_id: str,
    cycle: int,
    stage: str,
    required_action: str,
    action_kind: str = "",
) -> Dict[str, Any]:
    previous = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    normalized_action = str(required_action or "Take one edit, targeted validation, diff review, or terminal action.").strip()
    requested_kind = str(action_kind or resilience.action_kind_for_required_action(normalized_action) or "bounded").strip()
    # The execution-loop default predates action-kind tagging and begins with
    # "Make" rather than an edit verb recognized by the generic classifier.
    # Treat that specific remediation directive as edit-only even when an older
    # persisted working-memory record explicitly carries action_kind=bounded.
    if (
        requested_kind == "bounded"
        and normalized_action.casefold().startswith("make the smallest evidence-backed edit")
    ):
        requested_kind = "edit"
    normalized_kind = requested_kind if requested_kind in _ACTION_ALLOWED_TOOLS else "bounded"
    previous_kind = str(previous.get("action_kind") or "bounded").strip()
    if previous_kind not in _ACTION_ALLOWED_TOOLS:
        previous_kind = "bounded"
    same_state = str(previous.get("state_key") or "") == state_key
    same_directive = (
        same_state
        and str(previous.get("required_action") or "") == normalized_action
        and previous_kind == normalized_kind
    )
    previous_run = str(previous.get("run_id") or "")
    activation_count = int(previous.get("activation_count") or 0) + (
        0 if same_directive and previous_run == run_id else 1
    )
    resume_count = int(previous.get("resume_count") or 0)
    if same_directive and previous_run and previous_run != run_id:
        resume_count += 1
    now = time.time()
    event_count = len([item for item in (task.get("agent_events") or []) if isinstance(item, dict)])
    initial_allowed = _ACTION_ALLOWED_TOOLS[normalized_kind]
    return {
        "schema": SCHEMA,
        "status": "active",
        "state_key": state_key,
        "run_id": run_id,
        "cycle": int(cycle or 0),
        "stage": str(stage or "interrupt"),
        "required_action": normalized_action,
        "action_kind": normalized_kind,
        "allowed_tools": sorted(initial_allowed),
        # Kept for backwards-compatible diagnostics. Zero means the action
        # class remains available until durable progress or terminal escalation.
        "attempt_limit": 0,
        "activation_event_count": (
            int(previous.get("activation_event_count") or 0)
            if same_directive
            else event_count
        ),
        "activation_count": max(1, activation_count),
        "resume_count": resume_count,
        "rejection_limit": 2,
        "activated_at": float(previous.get("activated_at") or now) if same_directive else now,
        "updated_at": now,
    }


def retire_if_state_changed(task: Dict[str, Any], *, state_key: str) -> bool:
    current = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    if str(current.get("status") or "") != "active":
        return False
    if str(current.get("state_key") or "") == state_key:
        return False
    history = [item for item in (task.get("agent_forced_action_history") or []) if isinstance(item, dict)]
    retired = dict(current)
    retired.update({"status": "superseded", "superseded_by_state_key": state_key, "retired_at": time.time()})
    history.append(retired)
    task["agent_forced_action_history"] = history[-16:]
    task["agent_forced_action"] = retired
    return True


def allowed_tool_names(task: Mapping[str, Any]) -> set[str]:
    state = active_state(task)
    return set(state.get("allowed_tools") or []) if state else set()


def rejection_counter_for_state(
    previous_state_key: str,
    previous_count: int,
    task: Mapping[str, Any],
) -> tuple[str, int]:
    current_key = str(active_state(task).get("state_key") or "")
    if current_key != str(previous_state_key or ""):
        return current_key, 0
    return current_key, max(0, int(previous_count or 0))


def evaluate_tool_call(
    task: Mapping[str, Any],
    *,
    name: str,
    args: Mapping[str, Any],
    is_validation_command: Callable[[Any], bool],
) -> tuple[bool, Dict[str, Any]]:
    state = active_state(task)
    if not state:
        return True, {}
    tool_name = str(name or "").strip()
    allowed_tools = set(state.get("allowed_tools") or [])
    allowed = tool_name in allowed_tools
    if tool_name == "coding_run_command" and allowed:
        allowed = bool(is_validation_command(args.get("argv")))
    if allowed:
        return True, {}
    required_action = str(state.get("required_action") or "").strip()
    attempts = int(state.get("attempt_count") or 0)
    policy = (
        f"Only tools for the current {state.get('action_kind') or 'bounded'} action are enabled. "
        "Inspection, unrelated validation, and plan churn are disabled until durable progress or terminal escalation."
    )
    message = (
        f"Forced-action mode rejected {tool_name or '(missing tool name)'}. "
        f"{policy} Required action: {required_action}"
    )
    return False, {
        "ok": False,
        "error": "forced_action_tool_rejected",
        "message": message,
        "required_action": required_action,
        "action_kind": state.get("action_kind"),
        "attempt_count": attempts,
        "attempt_limit": 0,
        "allowed_tools": sorted(allowed_tools),
        "state_key": state.get("state_key"),
        "stage": state.get("stage"),
    }


def prompt_context(task: Mapping[str, Any]) -> str:
    state = active_state(task)
    if not state:
        return ""
    allowed = ", ".join(state.get("allowed_tools") or [])
    return (
        "Controller forced-action mode is ACTIVE for the unchanged durable state. "
        "Inspection, repository orientation, plan churn, and unrelated commands are unavailable. "
        "The required action class remains available until durable progress changes the state or terminal escalation ends the run. "
        f"Required action: {state.get('required_action') or ''} "
        f"Action kind: {state.get('action_kind') or 'bounded'}. "
        f"Available tools: {allowed or 'coding_finish'}."
    )


def filter_tool_specs(specs: Sequence[Any], task: Mapping[str, Any]) -> list[Any]:
    allowed = allowed_tool_names(task)
    if not allowed:
        return list(specs)
    out = []
    for spec in specs:
        try:
            name = str(spec.function.name)
        except Exception:
            continue
        if name in allowed:
            out.append(spec)
    return out
