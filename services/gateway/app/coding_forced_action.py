from __future__ import annotations

import re
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
_TARGETED_EVIDENCE_TOOLS = {"coding_search_text", "coding_read_file_lines"}
_ACTION_ALLOWED_TOOLS = {
    "evidence": {*_TARGETED_EVIDENCE_TOOLS, "coding_update_plan", "coding_finish"},
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
_EVIDENCE_EXECUTION_DIRECTIVE = (
    "Establish a remediation hypothesis before editing: gather targeted repository evidence, then record the hypothesis with coding_update_plan."
)
_HYPOTHESIS_FIELDS = (
    "Root cause",
    "Repository evidence",
    "Competing explanation checked",
    "Expected result",
)
_MAX_TARGETED_EVIDENCE_ACTIONS = 2


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


def _targeted_evidence_count(task: Mapping[str, Any], state: Mapping[str, Any]) -> int:
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
            if event_ts < activated_at:
                continue
        elif index < legacy_start:
            continue
        if str(event.get("type") or "") != "tool_finished":
            continue
        if str(event.get("name") or "").strip() not in _TARGETED_EVIDENCE_TOOLS:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False or str(result.get("error") or "") == "forced_action_tool_rejected":
            continue
        count += 1
    return count


def _plan_revision(task: Mapping[str, Any]) -> int:
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), dict) else {}
    try:
        return max(0, int(plan.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _project_plan_text(task: Mapping[str, Any]) -> str:
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), dict) else {}
    parts = [str(plan.get("goal") or ""), str(plan.get("note") or "")]
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts.extend(
            [
                str(item.get("id") or ""),
                str(item.get("title") or ""),
                str(item.get("summary") or ""),
            ]
        )
    return "\n".join(part for part in parts if part)


def _structured_hypothesis(task: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[bool, Dict[str, str]]:
    current_revision = _plan_revision(task)
    try:
        activation_revision = int(state.get("activation_plan_revision", -1))
    except (TypeError, ValueError):
        activation_revision = -1
    if current_revision <= activation_revision:
        return False, {}
    text = _project_plan_text(task)
    fields: Dict[str, str] = {}
    for index, label in enumerate(_HYPOTHESIS_FIELDS):
        next_label = _HYPOTHESIS_FIELDS[index + 1] if index + 1 < len(_HYPOTHESIS_FIELDS) else ""
        if next_label:
            pattern = rf"(?is){re.escape(label)}\s*:\s*(.+?)(?=\n\s*{re.escape(next_label)}\s*:|$)"
        else:
            pattern = rf"(?is){re.escape(label)}\s*:\s*(.+?)\s*$"
        match = re.search(pattern, text)
        value = str(match.group(1) if match else "").strip()
        if len(value) < 8:
            return False, fields
        fields[label] = value
    return True, fields


def _canonical_required_action(state: Mapping[str, Any]) -> str:
    return _normalize_required_action(state.get("canonical_required_action") or state.get("required_action"))


def _canonical_action_kind(state: Mapping[str, Any]) -> str:
    action = _canonical_required_action(state)
    return _normalize_action_kind(
        action,
        str(state.get("canonical_action_kind") or state.get("action_kind") or "bounded"),
    )


def _generic_edit_requires_hypothesis(state: Mapping[str, Any]) -> bool:
    if state.get("requires_hypothesis") is True:
        return True
    return _canonical_required_action(state).casefold() == _EDIT_EXECUTION_DIRECTIVE.casefold()


def _apply_hypothesis_gate(task: Mapping[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    if not _generic_edit_requires_hypothesis(state):
        return state
    evidence_count = _targeted_evidence_count(task, state)
    hypothesis_ready, fields = _structured_hypothesis(task, state)
    gate_ready = evidence_count > 0 and hypothesis_ready
    state["requires_hypothesis"] = True
    state["targeted_evidence_count"] = evidence_count
    state["targeted_evidence_limit"] = _MAX_TARGETED_EVIDENCE_ACTIONS
    state["hypothesis_ready"] = hypothesis_ready
    state["hypothesis_fields"] = sorted(fields)
    state["hypothesis_plan_revision"] = _plan_revision(task) if hypothesis_ready else None
    if gate_ready:
        state["required_action"] = _EDIT_EXECUTION_DIRECTIVE
        state["action_kind"] = "edit"
        state["allowed_tools"] = sorted(_ACTION_ALLOWED_TOOLS["edit"])
        return state
    state["required_action"] = _EVIDENCE_EXECUTION_DIRECTIVE
    state["action_kind"] = "evidence"
    evidence_tools = set(_ACTION_ALLOWED_TOOLS["evidence"])
    if evidence_count >= _MAX_TARGETED_EVIDENCE_ACTIONS:
        evidence_tools -= _TARGETED_EVIDENCE_TOOLS
    state["allowed_tools"] = sorted(evidence_tools)
    return state


def _effective_allowed_tools(state: Mapping[str, Any]) -> set[str]:
    # Forced-action mode is a tool-class restriction, not a single-tool lease.
    # Keep the required action class available until the durable state changes
    # or normal controller escalation terminates the run. Collapsing to
    # coding_finish after one tool call can deadlock unchanged resumes: the
    # workspace still requires an edit, while the only remaining action is a
    # finish call that the no-change audit must reject.
    kind = str(state.get("action_kind") or "bounded")
    allowed = set(_ACTION_ALLOWED_TOOLS.get(kind, _ACTION_ALLOWED_TOOLS["bounded"]))
    if kind == "evidence" and int(state.get("targeted_evidence_count") or 0) >= _MAX_TARGETED_EVIDENCE_ACTIONS:
        allowed -= _TARGETED_EVIDENCE_TOOLS
    return allowed


def _normalize_required_action(required_action: Any) -> str:
    normalized = str(required_action or "").strip()
    if normalized.casefold() == _GENERIC_EXECUTION_DIRECTIVE:
        return _EDIT_EXECUTION_DIRECTIVE
    return normalized


def _normalize_action_kind(required_action: str, action_kind: str) -> str:
    kind = str(action_kind or "bounded").strip()
    if kind not in _ACTION_ALLOWED_TOOLS:
        kind = "bounded"
    # The execution-loop defaults predate action-kind tagging and begin with
    # generic prose rather than edit verbs recognized by the classifier.
    # Normalize both newly requested and already-persisted records. Generic
    # remediation subsequently passes through the evidence/hypothesis gate.
    action = _normalize_required_action(required_action).casefold()
    if kind == "bounded" and action.startswith("make the smallest evidence-backed edit"):
        return "edit"
    return kind


def active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    state = _raw_active_state(task)
    if not state:
        return {}
    canonical_action = _canonical_required_action(state)
    canonical_kind = _canonical_action_kind(state)
    state["canonical_required_action"] = canonical_action
    state["canonical_action_kind"] = canonical_kind
    state["required_action"] = canonical_action
    state["action_kind"] = canonical_kind
    state = _apply_hypothesis_gate(task, state)
    attempts = _attempt_count(task, state)
    state["attempt_count"] = attempts
    if "allowed_tools" not in state:
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
    previous_action = _canonical_required_action(previous) if previous else ""
    previous_kind = _canonical_action_kind(previous) if previous else "bounded"
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
    requires_hypothesis = normalized_action.casefold() == _EDIT_EXECUTION_DIRECTIVE.casefold()
    state = {
        "schema": SCHEMA,
        "status": "active",
        "state_key": state_key,
        "run_id": run_id,
        "cycle": int(cycle or 0),
        "stage": str(stage or "interrupt"),
        "canonical_required_action": normalized_action,
        "canonical_action_kind": normalized_kind,
        "required_action": normalized_action,
        "action_kind": normalized_kind,
        "requires_hypothesis": requires_hypothesis,
        "activation_plan_revision": (
            int(previous.get("activation_plan_revision", _plan_revision(task)))
            if same_directive
            else _plan_revision(task)
        ),
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
    state = _apply_hypothesis_gate(task, state)
    if "allowed_tools" not in state:
        state["allowed_tools"] = sorted(_effective_allowed_tools(state))
    return state


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
    kind = str(state.get("action_kind") or "bounded")
    if kind == "evidence":
        policy = (
            "Only bounded evidence and remediation-hypothesis tools are enabled. "
            "Editing is disabled until targeted repository evidence and a structured hypothesis are recorded."
        )
    else:
        policy = (
            f"Only tools for the current {kind} action are enabled. "
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
        "canonical_required_action": state.get("canonical_required_action"),
        "action_kind": kind,
        "canonical_action_kind": state.get("canonical_action_kind"),
        "attempt_count": attempts,
        "attempt_limit": 0,
        "allowed_tools": sorted(allowed_tools),
        "state_key": state.get("state_key"),
        "stage": state.get("stage"),
        "hypothesis_ready": state.get("hypothesis_ready"),
        "targeted_evidence_count": state.get("targeted_evidence_count"),
    }


def prompt_context(task: Mapping[str, Any]) -> str:
    state = active_state(task)
    if not state:
        return ""
    allowed = ", ".join(state.get("allowed_tools") or [])
    kind = str(state.get("action_kind") or "bounded")
    if kind == "evidence":
        fields = "; ".join(f"{label}: <specific finding>" for label in _HYPOTHESIS_FIELDS)
        remaining = max(
            0,
            _MAX_TARGETED_EVIDENCE_ACTIONS - int(state.get("targeted_evidence_count") or 0),
        )
        return (
            "Controller forced-action mode is ACTIVE for the unchanged durable state, but editing is not yet authorized. "
            "The generic forced-mode prohibition on inspection/plan revision is superseded for this bounded evidence checkpoint. "
            f"Use at most {remaining} additional targeted evidence action(s) via coding_search_text or coding_read_file_lines. "
            "Then call coding_update_plan and record a remediation hypothesis in the plan note or milestone summary using these exact fields: "
            f"{fields}. Each field must contain concrete content. "
            "The Repository evidence field should identify the file/symbol/behavior that supports the causal claim; the competing explanation field should state what plausible alternative was checked. "
            "Do not edit merely because a likely file was found. Editing unlocks only after at least one successful targeted evidence action and a newly revised structured hypothesis. "
            f"Available tools: {allowed or 'coding_finish'}."
        )
    hypothesis_note = ""
    if state.get("requires_hypothesis"):
        hypothesis_note = (
            "The evidence-qualified remediation gate is satisfied for this durable state; perform only the smallest edit that tests the recorded causal hypothesis. "
        )
    return (
        "Controller forced-action mode is ACTIVE for the unchanged durable state. "
        "Inspection, repository orientation, plan churn, and unrelated commands are unavailable. "
        "The required action class remains available until durable progress changes the state or terminal escalation ends the run. "
        f"{hypothesis_note}"
        f"Required action: {state.get('required_action') or ''} "
        f"Action kind: {kind}. "
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
