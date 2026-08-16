from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, Mapping


_ACCEPTANCE_PARTS = {"tests", "test", "fixtures", "fixture", "examples", "example"}
_CONTEXT_SUFFIXES = {".md", ".rst", ".txt"}
_OVERRIDE_KEY = "_execution_forced_action_override"


def _event_after_activation(
    event: Mapping[str, Any],
    *,
    index: int,
    state: Mapping[str, Any],
) -> bool:
    try:
        activated_at = float(state.get("activated_at") or 0)
    except (TypeError, ValueError):
        activated_at = 0.0
    try:
        event_ts = float(event.get("ts") or 0)
    except (TypeError, ValueError):
        event_ts = 0.0
    if activated_at > 0 and event_ts > 0:
        return event_ts >= activated_at
    legacy_start = max(0, int(state.get("activation_event_count") or 0))
    return index >= legacy_start


def _path_class(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return "unknown"
    parsed = PurePosixPath(normalized)
    parts = {part.casefold() for part in parsed.parts}
    name = parsed.name.casefold()
    if parts & _ACCEPTANCE_PARTS or name.startswith("test_") or name.endswith("_test.py"):
        return "acceptance"
    if parsed.suffix.casefold() in _CONTEXT_SUFFIXES or "docs" in parts:
        return "context"
    return "causal"


def _path_from_result(result: Mapping[str, Any]) -> str:
    explicit = str(result.get("path") or "").strip()
    if explicit:
        return explicit
    matches = result.get("matches")
    if not isinstance(matches, list):
        return ""
    for item in matches:
        text = str(item or "").strip()
        if not text:
            continue
        candidate = text.split(":", 1)[0].strip()
        if "/" in candidate or candidate.endswith(('.py', '.js', '.ts', '.html', '.yaml', '.yml', '.json')):
            return candidate
    return ""


def _evidence_records(
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[dict[str, str]]:
    events = [
        item for item in (task.get("agent_events") or []) if isinstance(item, Mapping)
    ]
    started: dict[str, Mapping[str, Any]] = {}
    last_started_by_name: dict[str, Mapping[str, Any]] = {}
    records: list[dict[str, str]] = []
    for index, event in enumerate(events):
        if not _event_after_activation(event, index=index, state=state):
            continue
        event_type = str(event.get("type") or "")
        tool_call_id = str(event.get("tool_call_id") or "")
        if event_type == "tool_started":
            name = str(event.get("name") or "")
            if name in forced_action._TARGETED_EVIDENCE_TOOLS:
                started[tool_call_id or f"index:{index}"] = event
                last_started_by_name[name] = event
            continue
        if event_type != "tool_finished":
            continue
        name = str(event.get("name") or "")
        if name not in forced_action._TARGETED_EVIDENCE_TOOLS:
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if not forced_action._targeted_evidence_result_succeeded(name, result):
            continue
        source = started.get(tool_call_id) or last_started_by_name.get(name)
        args = (
            source.get("args")
            if isinstance(source, Mapping) and isinstance(source.get("args"), Mapping)
            else {}
        )
        path = str(_path_from_result(result) or args.get("path") or "").strip()
        records.append(
            {
                "tool": name,
                "path": path,
                "class": _path_class(path),
            }
        )
    return records


def _repository_evidence_links_target(
    repository_evidence: str,
    target: str,
) -> bool:
    evidence = str(repository_evidence or "").casefold()
    normalized = str(target or "").strip().replace("\\", "/")
    if not evidence or not normalized:
        return False
    basename = PurePosixPath(normalized).name.casefold()
    return normalized.casefold() in evidence or (basename and basename in evidence)


def apply_provenance_gate(
    forced_action: Any,
    task: Mapping[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    state = dict(state)
    if not state or not forced_action._generic_edit_requires_hypothesis(state):
        return state

    records = _evidence_records(forced_action, task, state)
    causal_targets = sorted(
        {
            record["path"]
            for record in records
            if record.get("class") == "causal" and record.get("path")
        }
    )
    acceptance_targets = sorted(
        {
            record["path"]
            for record in records
            if record.get("class") == "acceptance" and record.get("path")
        }
    )
    context_targets = sorted(
        {
            record["path"]
            for record in records
            if record.get("class") == "context" and record.get("path")
        }
    )
    hypothesis_ready, fields = forced_action._structured_hypothesis(task, state)
    repository_evidence = str(fields.get("Repository evidence") or "")
    linked_targets = [
        target
        for target in causal_targets
        if _repository_evidence_links_target(repository_evidence, target)
    ]
    provenance_ready = bool(linked_targets)

    state["evidence_provenance_enforced"] = True
    state["raw_targeted_evidence_count"] = int(state.get("targeted_evidence_count") or 0)
    state["causal_evidence_count"] = len(causal_targets)
    state["causal_evidence_targets"] = causal_targets
    state["acceptance_evidence_targets"] = acceptance_targets
    state["context_evidence_targets"] = context_targets
    state["hypothesis_causal_evidence_linked"] = provenance_ready
    state["hypothesis_causal_targets"] = linked_targets

    if state.get("action_kind") == "edit" and provenance_ready:
        return state

    state["action_kind"] = "evidence"
    state["required_action"] = (
        "Establish a remediation hypothesis from causal implementation/configuration "
        "evidence. Tests, fixtures, examples, and documentation may define acceptance "
        "criteria but do not by themselves establish root cause. Record Repository "
        "evidence that explicitly cites at least one inspected causal target."
    )
    evidence_tools = set(forced_action._ACTION_ALLOWED_TOOLS["evidence"])
    if causal_targets:
        # Causal evidence exists; force hypothesis revision rather than another
        # inspection cycle. If only acceptance/context evidence exists, restore
        # one deterministic causal-evidence path even if the legacy read budget
        # has already been consumed.
        evidence_tools -= forced_action._TARGETED_EVIDENCE_TOOLS
    state["allowed_tools"] = sorted(evidence_tools)
    state["hypothesis_ready"] = hypothesis_ready
    state["hypothesis_fields"] = sorted(fields)
    state["hypothesis_plan_revision"] = (
        forced_action._plan_revision(task) if hypothesis_ready else None
    )
    return state


def effective_state(forced_action: Any, task: Mapping[str, Any]) -> Dict[str, Any]:
    base = forced_action.active_state(task)
    if not base:
        return {}
    return apply_provenance_gate(forced_action, task, base)


def execution_task(forced_action: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    effective = effective_state(forced_action, task)
    if not effective:
        return dict(task)
    out = dict(task)
    out[_OVERRIDE_KEY] = effective
    return out


def install_execution_override_seam(forced_action: Any) -> None:
    """Let ephemeral execution copies supply a stricter effective forced state.

    Ordinary durable tasks continue through the original controller contract.
    This keeps policy refinement explicit at dispatch time instead of globally
    changing `active_state()` semantics for every importer/test in the process.
    """
    if bool(getattr(forced_action, "_execution_override_seam_installed", False)):
        return

    original_active_state = forced_action.active_state
    original_prompt_context = forced_action.prompt_context

    def active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
        override = task.get(_OVERRIDE_KEY) if isinstance(task, Mapping) else None
        if isinstance(override, Mapping):
            state = dict(override)
            state["attempt_count"] = forced_action._attempt_count(task, state)
            return state
        return original_active_state(task)

    def prompt_context(task: Mapping[str, Any]) -> str:
        override = task.get(_OVERRIDE_KEY) if isinstance(task, Mapping) else None
        if not isinstance(override, Mapping):
            return original_prompt_context(task)
        state = active_state(task)
        if (
            str(state.get("action_kind") or "") != "evidence"
            or not state.get("evidence_provenance_enforced")
        ):
            return original_prompt_context(task)

        allowed = ", ".join(state.get("allowed_tools") or [])
        causal_targets = list(state.get("causal_evidence_targets") or [])
        fields = "; ".join(
            f"{label}: <specific finding>" for label in forced_action._HYPOTHESIS_FIELDS
        )
        if not causal_targets:
            next_step = (
                "Use one targeted coding_search_text or coding_read_file_lines action "
                "against an implementation or configuration target. Tests, fixtures, "
                "examples, and documentation may define acceptance criteria but do not "
                "establish root cause."
            )
        else:
            next_step = (
                "Causal implementation/configuration evidence is already available. "
                "Do not spend the next action on more inspection; call coding_update_plan "
                "and explicitly cite at least one causal target in Repository evidence."
            )
        return (
            "Controller forced-action mode is ACTIVE for the unchanged durable state, "
            "but editing is not yet authorized. The execution policy applies an explicit "
            "causal-evidence provenance gate. "
            f"{next_step} Then record the remediation hypothesis using these exact fields: "
            f"{fields}. Available tools: {allowed or 'coding_finish'}."
        )

    forced_action.active_state = active_state
    forced_action.prompt_context = prompt_context
    forced_action._execution_override_seam_installed = True
