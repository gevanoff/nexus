from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Sequence

from app import coding_stagnation_resilience as resilience


SCHEMA = "nexus_coding_forced_action.v1"
_SOURCE_EVIDENCE_TOOLS = {
    "coding_read_file",
    "coding_read_file_lines",
    "coding_search_text",
}
_BASE_ALLOWED_TOOLS = {
    "coding_write_file",
    "coding_replace_text",
    "coding_apply_patch",
    "coding_run_command",
    "coding_git_diff",
    "coding_finish",
}
_ACTION_ALLOWED_TOOLS = {
    "evidence": {*_SOURCE_EVIDENCE_TOOLS, "coding_finish"},
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
_GENERIC_EXECUTION_DIRECTIVE = "take one bounded execution action, or finish with a concrete blocker."
_EDIT_EXECUTION_DIRECTIVE = "Make the smallest evidence-backed edit, or finish with a concrete blocker."
_EVIDENCE_DIRECTIVE_PREFIXES = (
    "gather one targeted piece of repository evidence",
    "establish bounded causal repository evidence",
)


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
    countable_tools = _BASE_ALLOWED_TOOLS | _SOURCE_EVIDENCE_TOOLS
    count = 0
    for index, event in enumerate(events):
        event_ts = _event_timestamp(event)
        if activated_at > 0 and event_ts > 0:
            if event_ts < activated_at:
                continue
        elif index < legacy_start:
            continue
        if str(event.get("type") or "") != "tool_finished":
            continue
        name = str(event.get("name") or "").strip()
        if name == "coding_finish" or name not in countable_tools:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if str(result.get("error") or "") == "forced_action_tool_rejected":
            continue
        count += 1
    return count


def _effective_allowed_tools(state: Mapping[str, Any]) -> set[str]:
    kind = str(state.get("action_kind") or "bounded")
    return set(_ACTION_ALLOWED_TOOLS.get(kind, _ACTION_ALLOWED_TOOLS["bounded"]))


def _normalize_required_action(required_action: Any) -> str:
    normalized = str(required_action or "").strip()
    if normalized.casefold() == _GENERIC_EXECUTION_DIRECTIVE:
        return _EDIT_EXECUTION_DIRECTIVE
    return normalized


def _normalize_action_kind(required_action: str, action_kind: str) -> str:
    kind = str(action_kind or "bounded").strip()
    if kind not in _ACTION_ALLOWED_TOOLS:
        kind = "bounded"
    action = _normalize_required_action(required_action).casefold()
    if any(action.startswith(prefix) for prefix in _EVIDENCE_DIRECTIVE_PREFIXES):
        return "evidence"
    if kind == "bounded" and action.startswith("make the smallest evidence-backed edit"):
        return "edit"
    return kind


def active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    state = _raw_active_state(task)
    if not state:
        return {}
    normalized_action = _normalize_required_action(state.get("required_action"))
    state["required_action"] = normalized_action
    state["action_kind"] = _normalize_action_kind(
        normalized_action,
        str(state.get("action_kind") or "bounded"),
    )
    attempts = _attempt_count(task, state)
    state["attempt_count"] = attempts
    state["allowed_tools"] = sorted(_effective_allowed_tools(state))
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
    normalized_action = _normalize_required_action(
        required_action or "Take one edit, targeted validation, diff review, or terminal action."
    )
    requested_kind = str(action_kind or resilience.action_kind_for_required_action(normalized_action) or "bounded").strip()
    normalized_kind = _normalize_action_kind(normalized_action, requested_kind)
    previous_action = _normalize_required_action(previous.get("required_action"))
    previous_kind = _normalize_action_kind(
        previous_action,
        str(previous.get("action_kind") or "bounded"),
    )
    same_state = str(previous.get("state_key") or "") == state_key
    same_directive = (
        same_state
        and previous_action == normalized_action
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
    action_kind = str(state.get("action_kind") or "bounded")
    if action_kind == "evidence":
        policy = (
            "Only targeted source-evidence reads/searches and coding_finish are enabled. "
            "Edits, broad orientation, validation, diff review, and plan churn remain disabled until the evidence gate succeeds."
        )
    else:
        policy = (
            f"Only tools for the current {action_kind} action are enabled. "
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
    if str(state.get("action_kind") or "") == "evidence":
        availability = (
            "Targeted repository source reads/searches are available for this evidence action; "
            "editing and unrelated actions are unavailable until the evidence gate succeeds. "
        )
    else:
        availability = (
            "Inspection, repository orientation, plan churn, and unrelated commands are unavailable. "
        )
    return (
        "Controller forced-action mode is ACTIVE for the unchanged durable state. "
        f"{availability}"
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