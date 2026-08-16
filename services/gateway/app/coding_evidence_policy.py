from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, Mapping


_ACCEPTANCE_PARTS = {"tests", "test", "fixtures", "fixture", "examples", "example"}
_CONTEXT_SUFFIXES = {".md", ".rst", ".txt"}


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


def _evidence_records(
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[dict[str, str]]:
    events = [
        item for item in (task.get("agent_events") or []) if isinstance(item, Mapping)
    ]
    started: dict[str, Mapping[str, Any]] = {}
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
            continue
        if event_type != "tool_finished":
            continue
        name = str(event.get("name") or "")
        if name not in forced_action._TARGETED_EVIDENCE_TOOLS:
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if not forced_action._targeted_evidence_result_succeeded(name, result):
            continue
        source = started.get(tool_call_id)
        args = (
            source.get("args")
            if isinstance(source, Mapping) and isinstance(source.get("args"), Mapping)
            else {}
        )
        path = str(result.get("path") or args.get("path") or "").strip()
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
    if not forced_action._generic_edit_requires_hypothesis(state):
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
    hypothesis_ready, fields = forced_action._structured_hypothesis(task, state)
    repository_evidence = str(fields.get("Repository evidence") or "")
    linked_targets = [
        target
        for target in causal_targets
        if _repository_evidence_links_target(repository_evidence, target)
    ]
    provenance_ready = bool(linked_targets)

    state["causal_evidence_count"] = len(causal_targets)
    state["causal_evidence_targets"] = causal_targets
    state["acceptance_evidence_targets"] = acceptance_targets
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
        # Causal evidence exists; force the next move to link it into the
        # hypothesis instead of spending the bounded window on more reads.
        evidence_tools -= forced_action._TARGETED_EVIDENCE_TOOLS
    state["allowed_tools"] = sorted(evidence_tools)
    state["hypothesis_ready"] = hypothesis_ready
    state["hypothesis_fields"] = sorted(fields)
    state["hypothesis_plan_revision"] = (
        forced_action._plan_revision(task) if hypothesis_ready else None
    )
    return state


def install(forced_action: Any) -> None:
    if bool(getattr(forced_action, "_provenance_gate_installed", False)):
        return

    original_apply = forced_action._apply_hypothesis_gate
    original_prompt_context = forced_action.prompt_context

    def apply_hypothesis_gate(
        task: Mapping[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        state = original_apply(task, state)
        return apply_provenance_gate(forced_action, task, state)

    def prompt_context(task: Mapping[str, Any]) -> str:
        state = forced_action.active_state(task)
        if not state or str(state.get("action_kind") or "") != "evidence":
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
            "but editing is not yet authorized. "
            f"{next_step} Then record the remediation hypothesis using these exact fields: "
            f"{fields}. Available tools: {allowed or 'coding_finish'}."
        )

    forced_action._apply_hypothesis_gate = apply_hypothesis_gate
    forced_action.prompt_context = prompt_context
    forced_action._provenance_gate_installed = True
