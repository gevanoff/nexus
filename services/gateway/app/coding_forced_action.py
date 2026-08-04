from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Sequence

from app import coding_stagnation_resilience as resilience


SCHEMA = "nexus_coding_forced_action.v1"
_ALLOWED_TOOLS = {
    "coding_write_file",
    "coding_replace_text",
    "coding_apply_patch",
    "coding_run_command",
    "coding_git_diff",
    "coding_finish",
}


def active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    raw = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    if str(raw.get("schema") or "") != SCHEMA or str(raw.get("status") or "") != "active":
        return {}
    if str(raw.get("state_key") or "") != resilience.durable_state_key(task):
        return {}
    return dict(raw)


def activate(
    task: Mapping[str, Any],
    *,
    state_key: str,
    run_id: str,
    cycle: int,
    stage: str,
    required_action: str,
) -> Dict[str, Any]:
    previous = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    same_state = str(previous.get("state_key") or "") == state_key
    previous_run = str(previous.get("run_id") or "")
    activation_count = int(previous.get("activation_count") or 0) + (0 if same_state and previous_run == run_id else 1)
    resume_count = int(previous.get("resume_count") or 0)
    if same_state and previous_run and previous_run != run_id:
        resume_count += 1
    now = time.time()
    return {
        "schema": SCHEMA,
        "status": "active",
        "state_key": state_key,
        "run_id": run_id,
        "cycle": int(cycle or 0),
        "stage": str(stage or "interrupt"),
        "required_action": str(required_action or "Take one edit, targeted validation, diff review, or terminal action.").strip(),
        "allowed_tools": sorted(_ALLOWED_TOOLS),
        "activation_count": max(1, activation_count),
        "resume_count": resume_count,
        "rejection_limit": 2,
        "activated_at": float(previous.get("activated_at") or now) if same_state else now,
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
    return set(_ALLOWED_TOOLS) if active_state(task) else set()


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
    allowed = tool_name in _ALLOWED_TOOLS
    if tool_name == "coding_run_command":
        allowed = bool(is_validation_command(args.get("argv")))
    if allowed:
        return True, {}
    required_action = str(state.get("required_action") or "").strip()
    message = (
        f"Forced-action mode rejected {tool_name or '(missing tool name)'}. "
        "Inspection and arbitrary shell commands are disabled for this unchanged durable state. "
        f"Required action: {required_action}"
    )
    return False, {
        "ok": False,
        "error": "forced_action_tool_rejected",
        "message": message,
        "required_action": required_action,
        "allowed_tools": sorted(_ALLOWED_TOOLS),
        "state_key": state.get("state_key"),
        "stage": state.get("stage"),
    }


def prompt_context(task: Mapping[str, Any]) -> str:
    state = active_state(task)
    if not state:
        return ""
    return (
        "Controller forced-action mode is ACTIVE for the unchanged durable state. "
        "Inspection tools, repository orientation, plan churn, and arbitrary shell commands are unavailable. "
        f"Required action: {state.get('required_action') or ''} "
        "Use exactly one of: a focused edit, a recognized validation command, coding_git_diff, or coding_finish."
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
