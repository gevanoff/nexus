from __future__ import annotations

import json
import re
from typing import Any, Dict, Mapping


_ERROR_RANGE_UNVERIFIED = "coding_update_plan_hypothesis_range_unverified"
_ERROR_NOOP_EDIT = "coding_noop_edit"
_BARE_TOOL_START_RE = re.compile(
    r"(?m)^[ \t]*(coding_[A-Za-z_][A-Za-z0-9_]*)[ \t]*(?=\{)"
)


def _verified_ranges(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in state.get("causal_evidence_ranges") or []:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip()
        try:
            start = int(item.get("start_line"))
            end = int(item.get("end_line"))
        except (TypeError, ValueError):
            continue
        if path and start > 0 and end >= start:
            out.append({"path": path, "start_line": start, "end_line": end})
    return out


def _render_ranges(state: Mapping[str, Any]) -> str:
    return ", ".join(
        f"{item['path']}:{item['start_line']}-{item['end_line']}"
        for item in _verified_ranges(state)
    )


def _range_error(
    persistence: Any,
    agent: Any,
    state: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> Dict[str, Any]:
    base = persistence._base_policy(agent)
    return {
        "ok": False,
        "error": _ERROR_RANGE_UNVERIFIED,
        "message": (
            "Repository evidence cited at least one bounded repository target without a line span contained "
            "in the actual completed read. Every bounded target named as Repository evidence must be backed "
            "by an actual completed span; a second valid citation cannot authorize an invalid one. Revise "
            "coding_update_plan.note using the returned verified_causal_ranges. Requested read bounds are "
            "not authoritative when the file ended earlier."
        ),
        "required_hypothesis_fields": [str(label) for label in base._HYPOTHESIS_FIELDS],
        "missing_hypothesis_fields": [],
        "verified_causal_targets": list(persistence._verified_targets(state)),
        "verified_causal_ranges": list(
            validation.get("verified_ranges") or _verified_ranges(state)
        ),
        "unverified_bounded_targets": list(
            validation.get("missing_range_targets") or []
        ),
        "required_action": str(state.get("required_action") or ""),
    }


def _augment_tool_spec(
    agent: Any,
    persistence: Any,
    spec: Any,
    state: Mapping[str, Any],
) -> Any:
    try:
        if str(spec.function.name) != "coding_update_plan":
            return spec
    except Exception:
        return spec
    rendered = _render_ranges(state)
    if not rendered:
        return spec

    parameters = dict(spec.function.parameters or {})
    properties = dict(parameters.get("properties") or {})
    note = dict(properties.get("note") or {})
    note["description"] = (
        str(note.get("description") or "").rstrip()
        + "\nFor bounded reads, every Repository evidence target you cite must include an actual "
        f"verified span using path:start-end syntax. Verified spans: {rendered}."
    )
    properties["note"] = note
    parameters["properties"] = properties
    description = (
        str(spec.function.description or "").rstrip()
        + " For bounded reads, cite actual completed spans rather than requested read bounds. "
        + "If Repository evidence names multiple bounded targets, every named target must validate. "
        f"Verified spans: {rendered}."
    )
    return agent.ToolSpec(
        function=agent.ToolFunction(
            name="coding_update_plan",
            description=description,
            parameters=parameters,
        )
    )


def _known_tool_names(agent: Any) -> set[str]:
    getter = getattr(agent, "_tool_specs", None)
    if not callable(getter):
        return set()
    names: set[str] = set()
    try:
        specs = list(getter())
    except Exception:
        return set()
    for spec in specs:
        try:
            name = str(spec.function.name or "").strip()
        except Exception:
            name = ""
        if name:
            names.add(name)
    return names


def _extract_bare_text_tool_calls(agent: Any, content: Any) -> list[Dict[str, Any]]:
    """Recover one unambiguous trailing ``coding_tool{...}`` call.

    Some text-form vLLM fallbacks copy the authorized tool name and JSON arguments
    correctly but omit the required ``<tool_call>`` wrapper. Treat that narrow
    serialization error as recoverable. Multiple candidates, malformed JSON,
    trailing prose, or unknown tool names remain prose-only and are never run.
    """
    text = content if isinstance(content, str) else ""
    if not text or "<tool_call>" in text.casefold():
        return []
    candidates = list(_BARE_TOOL_START_RE.finditer(text))
    if len(candidates) != 1:
        return []
    match = candidates[0]
    name = str(match.group(1) or "").strip()
    if not name or name not in _known_tool_names(agent):
        return []
    payload = text[match.end() :].lstrip()
    if not payload.startswith("{"):
        return []
    try:
        parsed, end = json.JSONDecoder().raw_decode(payload)
    except Exception:
        return []
    if payload[end:].strip() or not isinstance(parsed, dict):
        return []
    builder = getattr(agent, "_text_tool_call", None)
    if not callable(builder):
        return []
    return [builder(name, parsed)]


def _install_bare_text_tool_recovery(agent: Any) -> None:
    if bool(getattr(agent, "_coding_bare_text_tool_recovery_installed", False)):
        return
    original = getattr(agent, "_extract_text_tool_calls", None)
    if not callable(original):
        return

    def extract_text_tool_calls_with_bare_recovery(content: Any) -> list[Dict[str, Any]]:
        calls = list(original(content))
        if calls:
            return calls
        return _extract_bare_text_tool_calls(agent, content)

    agent._extract_text_tool_calls = extract_text_tool_calls_with_bare_recovery
    agent._extract_text_tool_calls_before_bare_recovery = original
    agent._coding_bare_text_tool_recovery_installed = True


def _noop_replace_error(args: Mapping[str, Any]) -> Dict[str, Any] | None:
    if "old_text" not in args or "new_text" not in args:
        return None
    old_text = str(args.get("old_text") or "")
    new_text = str(args.get("new_text") or "")
    if old_text != new_text:
        return None
    return {
        "ok": False,
        "error": _ERROR_NOOP_EDIT,
        "message": (
            "coding_replace_text cannot make progress when old_text and new_text are identical. "
            "Provide a materially changed new_text, use another authorized edit tool, or call "
            "coding_finish with a concrete blocker."
        ),
        "path": str(args.get("path") or "").strip(),
        "replacements": 0,
        "no_op": True,
    }


def _install_noop_failed_edit_exemption() -> None:
    from app import coding_failed_edit_recovery as failed_edit_recovery

    if bool(
        getattr(
            failed_edit_recovery,
            "_coding_noop_failed_edit_exemption_installed",
            False,
        )
    ):
        return
    original = getattr(failed_edit_recovery, "_failed_context_edit", None)
    if not callable(original):
        return

    def failed_context_edit_without_noop(
        agent: Any,
        events: Any,
        index: int,
        event: Mapping[str, Any],
    ) -> Dict[str, Any] | None:
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if str(result.get("error") or "").strip() == _ERROR_NOOP_EDIT:
            return None
        return original(agent, events, index, event)

    failed_edit_recovery._failed_context_edit = failed_context_edit_without_noop
    failed_edit_recovery._failed_context_edit_before_noop_exemption = original
    failed_edit_recovery._coding_noop_failed_edit_exemption_installed = True


def install(
    agent: Any,
    evidence_policy: Any,
    range_provenance: Any,
    persistence: Any,
    guarded_agent: Any = None,
) -> None:
    """Close bounded-evidence persistence and narrow text-tool serialization gaps."""
    if bool(getattr(agent, "_coding_hypothesis_range_contract_installed", False)):
        return

    _install_bare_text_tool_recovery(agent)
    _install_noop_failed_edit_exemption()

    original_specs_for_task = agent._tool_specs_for_task

    def specs_for_task_with_ranges(task: Dict[str, Any]) -> list[Any]:
        specs = list(original_specs_for_task(task))
        state = agent.forced_action.active_state(task)
        if not persistence._contract_required(state) or not _verified_ranges(state):
            return specs
        return [
            _augment_tool_spec(agent, persistence, spec, state)
            for spec in specs
        ]

    agent._tool_specs_for_task = specs_for_task_with_ranges

    original_run_tool = agent._run_tool

    def run_tool_with_range_contract(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        tool_name = str(name or "")
        if tool_name == "coding_replace_text":
            noop = _noop_replace_error(args)
            if noop is not None:
                return noop
        if tool_name != "coding_update_plan":
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        task = agent.cw.load_task(task_id)
        state = agent.forced_action.active_state(task)
        ranges = _verified_ranges(state)
        if not persistence._contract_required(state) or not ranges:
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        # Let the existing persistence contract own malformed/missing fields and
        # unexpected arguments. Range validation starts only once the note is
        # otherwise structurally valid and path-linked.
        if sorted(key for key in args if key != "note"):
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )
        base = persistence._base_policy(agent)
        fields, missing = persistence._parse_hypothesis_note(
            base,
            str(args.get("note") or ""),
        )
        if missing:
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        repository_evidence = str(fields.get("Repository evidence") or "")
        linked = persistence._linked_targets(
            evidence_policy,
            repository_evidence,
            state,
        )
        if not linked:
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        validation = range_provenance.validate_repository_evidence(
            evidence_policy,
            agent.forced_action,
            task,
            state,
            repository_evidence,
            targets=linked,
        )
        # A hypothesis that names multiple bounded targets must validate every
        # one. This closes the mixed-citation loophole where a valid citation on
        # one file could otherwise authorize an EOF-overrun citation on another.
        if (
            validation.get("missing_range_targets")
            or not validation.get("matched_targets")
        ):
            return _range_error(persistence, agent, state, validation)

        return original_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )

    agent._run_tool = run_tool_with_range_contract
    if (
        guarded_agent is not None
        and getattr(
            guarded_agent,
            "_run_tool_with_semantic_acceptance",
            None,
        )
        is original_run_tool
    ):
        guarded_agent._run_tool_with_semantic_acceptance = run_tool_with_range_contract
    agent._coding_hypothesis_range_contract_installed = True
    agent._run_tool_before_hypothesis_range_contract = original_run_tool
