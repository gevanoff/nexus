from __future__ import annotations

from typing import Any, Dict, Mapping


_ERROR_RANGE_UNVERIFIED = "coding_update_plan_hypothesis_range_unverified"


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


def _range_error(persistence: Any, agent: Any, state: Mapping[str, Any], validation: Mapping[str, Any]) -> Dict[str, Any]:
    base = persistence._base_policy(agent)
    return {
        "ok": False,
        "error": _ERROR_RANGE_UNVERIFIED,
        "message": (
            "Repository evidence cited a repository path but not a line span contained in the actual "
            "completed bounded read. Revise coding_update_plan.note to cite one of the returned "
            "verified_causal_ranges exactly or a narrower span inside it. Requested read bounds are not "
            "authoritative when the file ended earlier."
        ),
        "required_hypothesis_fields": [str(label) for label in base._HYPOTHESIS_FIELDS],
        "missing_hypothesis_fields": [],
        "verified_causal_targets": list(persistence._verified_targets(state)),
        "verified_causal_ranges": list(validation.get("verified_ranges") or _verified_ranges(state)),
        "required_action": str(state.get("required_action") or ""),
    }


def _augment_tool_spec(agent: Any, persistence: Any, spec: Any, state: Mapping[str, Any]) -> Any:
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
        + "\nFor bounded reads, Repository evidence must cite an actual verified span using "
        f"path:start-end syntax. Verified spans: {rendered}."
    )
    properties["note"] = note
    parameters["properties"] = properties
    description = (
        str(spec.function.description or "").rstrip()
        + " For bounded reads, cite an actual completed span rather than the requested read bounds. "
        f"Verified spans: {rendered}."
    )
    return agent.ToolSpec(
        function=agent.ToolFunction(
            name="coding_update_plan",
            description=description,
            parameters=parameters,
        )
    )


def install(
    agent: Any,
    evidence_policy: Any,
    range_provenance: Any,
    persistence: Any,
    guarded_agent: Any = None,
) -> None:
    """Close the bounded-evidence contract before a hypothesis note is persisted."""
    if bool(getattr(agent, "_coding_hypothesis_range_contract_installed", False)):
        return

    original_specs_for_task = agent._tool_specs_for_task

    def specs_for_task_with_ranges(task: Dict[str, Any]) -> list[Any]:
        specs = list(original_specs_for_task(task))
        state = agent.forced_action.active_state(task)
        if not persistence._contract_required(state) or not _verified_ranges(state):
            return specs
        return [_augment_tool_spec(agent, persistence, spec, state) for spec in specs]

    agent._tool_specs_for_task = specs_for_task_with_ranges

    original_run_tool = agent._run_tool

    def run_tool_with_range_contract(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        if str(name or "") != "coding_update_plan":
            return original_run_tool(task_id, name, args, git_token_value=git_token_value)

        task = agent.cw.load_task(task_id)
        state = agent.forced_action.active_state(task)
        ranges = _verified_ranges(state)
        if not persistence._contract_required(state) or not ranges:
            return original_run_tool(task_id, name, args, git_token_value=git_token_value)

        # Let the existing persistence contract own malformed/missing fields and
        # unexpected arguments. Range validation starts only once the note is
        # otherwise structurally valid and path-linked.
        if sorted(key for key in args if key != "note"):
            return original_run_tool(task_id, name, args, git_token_value=git_token_value)
        base = persistence._base_policy(agent)
        fields, missing = persistence._parse_hypothesis_note(base, str(args.get("note") or ""))
        if missing:
            return original_run_tool(task_id, name, args, git_token_value=git_token_value)

        repository_evidence = str(fields.get("Repository evidence") or "")
        linked = persistence._linked_targets(evidence_policy, repository_evidence, state)
        if not linked:
            return original_run_tool(task_id, name, args, git_token_value=git_token_value)

        validation = range_provenance.validate_repository_evidence(
            evidence_policy,
            agent.forced_action,
            task,
            state,
            repository_evidence,
            targets=linked,
        )
        if not bool(validation.get("ok")):
            return _range_error(persistence, agent, state, validation)

        return original_run_tool(task_id, name, args, git_token_value=git_token_value)

    agent._run_tool = run_tool_with_range_contract
    if (
        guarded_agent is not None
        and getattr(guarded_agent, "_run_tool_with_semantic_acceptance", None) is original_run_tool
    ):
        guarded_agent._run_tool_with_semantic_acceptance = run_tool_with_range_contract
    agent._coding_hypothesis_range_contract_installed = True
    agent._run_tool_before_hypothesis_range_contract = original_run_tool
