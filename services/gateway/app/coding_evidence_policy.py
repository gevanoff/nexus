from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, Mapping, Sequence


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
    stem = parsed.stem.casefold()
    conventional_test_name = (
        name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or stem.endswith("_test")
        or stem.endswith("_spec")
    )
    if parts & _ACCEPTANCE_PARTS or conventional_test_name:
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
        if "/" in candidate or candidate.endswith(
            (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".html", ".yaml", ".yml", ".json")
        ):
            return candidate
    return ""


def _base_policy(forced_action: Any) -> Any:
    return getattr(forced_action, "_execution_provenance_base", forced_action)


def _evidence_records(
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[dict[str, str]]:
    base = _base_policy(forced_action)
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
            if name in base._TARGETED_EVIDENCE_TOOLS:
                started[tool_call_id or f"index:{index}"] = event
                last_started_by_name[name] = event
            continue
        if event_type != "tool_finished":
            continue
        name = str(event.get("name") or "")
        if name not in base._TARGETED_EVIDENCE_TOOLS:
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if not base._targeted_evidence_result_succeeded(name, result):
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
    base = _base_policy(forced_action)
    state = dict(state)
    if not state or not base._generic_edit_requires_hypothesis(state):
        return state

    records = _evidence_records(base, task, state)
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
    hypothesis_ready, fields = base._structured_hypothesis(task, state)
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
    evidence_tools = set(base._ACTION_ALLOWED_TOOLS["evidence"])
    if causal_targets:
        # Causal evidence exists; force hypothesis revision rather than another
        # inspection cycle. If only acceptance/context evidence exists, restore
        # one deterministic causal-evidence path even if the legacy read budget
        # has already been consumed.
        evidence_tools -= base._TARGETED_EVIDENCE_TOOLS
    state["allowed_tools"] = sorted(evidence_tools)
    state["hypothesis_ready"] = hypothesis_ready
    state["hypothesis_fields"] = sorted(fields)
    state["hypothesis_plan_revision"] = (
        base._plan_revision(task) if hypothesis_ready else None
    )
    return state


def effective_state(forced_action: Any, task: Mapping[str, Any]) -> Dict[str, Any]:
    base = _base_policy(forced_action)
    override = task.get(_OVERRIDE_KEY) if isinstance(task, Mapping) else None
    if isinstance(override, Mapping):
        state = dict(override)
        state["attempt_count"] = base._attempt_count(task, state)
        return state
    current = base.active_state(task)
    if not current:
        return {}
    return apply_provenance_gate(base, task, current)


def execution_task(forced_action: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    effective = effective_state(forced_action, task)
    if not effective:
        return dict(task)
    out = dict(task)
    out[_OVERRIDE_KEY] = effective
    return out


def _provenance_prompt_context(base: Any, state: Mapping[str, Any]) -> str:
    allowed = ", ".join(state.get("allowed_tools") or [])
    causal_targets = list(state.get("causal_evidence_targets") or [])
    fields = "; ".join(
        f"{label}: <specific finding>" for label in base._HYPOTHESIS_FIELDS
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


class ExecutionForcedActionFacade:
    """Coding-Agent-local view of forced-action policy.

    The durable controller module remains unchanged for other importers. The
    Coding Agent gets one provenance-qualified state for prompt construction,
    advertised tools, and execution-time authorization so an unadvertised tool
    call cannot bypass the effective policy.
    """

    def __init__(self, base: Any) -> None:
        self._execution_provenance_base = _base_policy(base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._execution_provenance_base, name)

    def active_state(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        return effective_state(self._execution_provenance_base, task)

    def allowed_tool_names(self, task: Mapping[str, Any]) -> set[str]:
        state = self.active_state(task)
        return set(state.get("allowed_tools") or []) if state else set()

    def filter_tool_specs(
        self,
        specs: Sequence[Any],
        task: Mapping[str, Any],
    ) -> list[Any]:
        state = self.active_state(task)
        if not state:
            return list(specs)
        allowed = set(state.get("allowed_tools") or [])
        out = []
        for spec in specs:
            try:
                name = str(spec.function.name)
            except Exception:
                continue
            if name in allowed:
                out.append(spec)
        return out

    def prompt_context(self, task: Mapping[str, Any]) -> str:
        state = self.active_state(task)
        if not state:
            return ""
        if (
            str(state.get("action_kind") or "") == "evidence"
            and state.get("evidence_provenance_enforced")
        ):
            return _provenance_prompt_context(self._execution_provenance_base, state)
        return self._execution_provenance_base.prompt_context(task)

    def evaluate_tool_call(
        self,
        task: Mapping[str, Any],
        *,
        name: str,
        args: Mapping[str, Any],
        is_validation_command: Any,
    ) -> tuple[bool, Dict[str, Any]]:
        state = self.active_state(task)
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
        policy = (
            "Only causal-evidence and remediation-hypothesis tools are enabled. Editing is disabled until implementation/configuration evidence is explicitly linked to the structured hypothesis."
            if state.get("evidence_provenance_enforced") and kind == "evidence"
            else (
                "Only bounded evidence and remediation-hypothesis tools are enabled. Editing is disabled until targeted repository evidence and a structured hypothesis are recorded."
                if kind == "evidence"
                else (
                    f"Only tools for the current {kind} action are enabled. Inspection, "
                    "unrelated validation, and plan churn are disabled until durable progress or terminal escalation."
                )
            )
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
            "causal_evidence_targets": list(state.get("causal_evidence_targets") or []),
            "acceptance_evidence_targets": list(
                state.get("acceptance_evidence_targets") or []
            ),
            "hypothesis_causal_evidence_linked": bool(
                state.get("hypothesis_causal_evidence_linked")
            ),
        }


def install_execution_override_seam(agent: Any) -> None:
    """Install a Coding-Agent-local provenance-qualified policy facade."""
    if bool(getattr(agent, "_execution_forced_action_facade_installed", False)):
        return
    agent.forced_action = ExecutionForcedActionFacade(agent.forced_action)
    agent._execution_forced_action_facade_installed = True
