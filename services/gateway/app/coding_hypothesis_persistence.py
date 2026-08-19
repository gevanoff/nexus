from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence


_ERROR_REQUIRED = "coding_update_plan_hypothesis_required"
_ERROR_UNLINKED = "coding_update_plan_hypothesis_unlinked"
_ERROR_NOT_PERSISTED = "coding_update_plan_hypothesis_not_persisted"


def _base_policy(agent: Any) -> Any:
    forced = agent.forced_action
    return getattr(forced, "_execution_provenance_base", forced)


def _contract_required(state: Mapping[str, Any]) -> bool:
    return bool(
        str(state.get("action_kind") or "") == "evidence"
        and state.get("evidence_provenance_enforced")
        and state.get("causal_evidence_targets")
        and not state.get("hypothesis_causal_evidence_linked")
        and "coding_update_plan" in set(state.get("allowed_tools") or [])
    )


def _parse_hypothesis_note(base: Any, note: str) -> tuple[Dict[str, str], list[str]]:
    labels = tuple(str(label) for label in base._HYPOTHESIS_FIELDS)
    text = str(note or "").strip()
    matches = list(base._HYPOTHESIS_FIELD_RE.finditer(text))
    fields: Dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = str(match.group(1) or "").strip()
        label = next(
            (candidate for candidate in labels if candidate.casefold() == raw_label.casefold()),
            "",
        )
        if not label or label in fields:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip(" \t\r\n;.")
        if len(value) >= 8:
            fields[label] = value
    missing = [label for label in labels if label not in fields]
    return fields, missing


def _canonical_note(base: Any, fields: Mapping[str, str]) -> str:
    return "\n".join(
        f"{label}: {str(fields.get(label) or '').strip()}"
        for label in base._HYPOTHESIS_FIELDS
    )


def _verified_targets(state: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            str(target).strip().replace("\\", "/").strip("/")
            for target in (state.get("causal_evidence_targets") or [])
            if str(target).strip()
        }
    )


def _linked_targets(
    evidence_policy: Any,
    repository_evidence: str,
    state: Mapping[str, Any],
) -> list[str]:
    checker = getattr(evidence_policy, "_repository_evidence_links_target", None)
    if not callable(checker):
        return []
    return [
        target
        for target in _verified_targets(state)
        if checker(repository_evidence, target)
    ]


def _contract_error(
    *,
    error: str,
    message: str,
    base: Any,
    state: Mapping[str, Any],
    missing_fields: Sequence[str] = (),
) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "message": message,
        "required_hypothesis_fields": [str(label) for label in base._HYPOTHESIS_FIELDS],
        "missing_hypothesis_fields": [str(label) for label in missing_fields],
        "verified_causal_targets": _verified_targets(state),
        "required_action": str(state.get("required_action") or ""),
    }


def _contract_tool_spec(agent: Any, spec: Any, state: Mapping[str, Any]) -> Any:
    try:
        if str(spec.function.name) != "coding_update_plan":
            return spec
    except Exception:
        return spec

    base = _base_policy(agent)
    targets = _verified_targets(state)
    labels = [str(label) for label in base._HYPOTHESIS_FIELDS]
    template = "\n".join(f"{label}: <specific finding>" for label in labels)
    target_text = ", ".join(targets)
    description = (
        "Persist the structured remediation hypothesis into the durable project plan. "
        "During this evidence-to-edit transition, assistant prose does not count as a persisted hypothesis. "
        "Call this tool with ONLY the note argument; do not rewrite goal, items, or milestone summaries. "
        "The note must contain all four labelled hypothesis fields and Repository evidence must cite at least "
        f"one exact verified repository-relative target: {target_text}."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "required": ["note"],
        "properties": {
            "note": {
                "type": "string",
                "minLength": 32,
                "description": (
                    "Durable structured hypothesis. Use these exact labelled fields, preferably one per line:\n"
                    + template
                    + "\nRepository evidence must name an exact verified repository-relative target."
                ),
            }
        },
    }
    return agent.ToolSpec(
        function=agent.ToolFunction(
            name="coding_update_plan",
            description=description,
            parameters=parameters,
        )
    )


def _augment_prompt(original: str, base: Any, state: Mapping[str, Any]) -> str:
    if not _contract_required(state):
        return original
    labels = ", ".join(str(label) for label in base._HYPOTHESIS_FIELDS)
    targets = ", ".join(_verified_targets(state))
    return (
        str(original or "").rstrip()
        + "\nPersistence contract: Do not merely state the remediation hypothesis in assistant prose. "
        "Call coding_update_plan with ONLY its note argument. The note must persist these exact labelled "
        f"fields: {labels}. Repository evidence must cite at least one exact verified target: {targets}. "
        "Do not update goal, items, or milestone summaries during this transition."
    )


def install(agent: Any, evidence_policy: Any, guarded_agent: Any = None) -> None:
    """Install a durable, machine-verifiable hypothesis handoff for Coding Workspace."""
    if bool(getattr(agent, "_coding_hypothesis_persistence_installed", False)):
        return

    original_specs_for_task = agent._tool_specs_for_task

    def specs_for_task_with_contract(task: Dict[str, Any]) -> list[Any]:
        specs = list(original_specs_for_task(task))
        state = agent.forced_action.active_state(task)
        if not _contract_required(state):
            return specs
        return [_contract_tool_spec(agent, spec, state) for spec in specs]

    agent._tool_specs_for_task = specs_for_task_with_contract

    original_prompt_context = evidence_policy._provenance_prompt_context

    def prompt_context_with_persistence(base: Any, state: Mapping[str, Any]) -> str:
        return _augment_prompt(original_prompt_context(base, state), base, state)

    evidence_policy._provenance_prompt_context = prompt_context_with_persistence

    original_run_tool = agent._run_tool

    def run_tool_with_hypothesis_persistence(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        if str(name or "") != "coding_update_plan":
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        task = agent.cw.load_task(task_id)
        state = agent.forced_action.active_state(task)
        if not _contract_required(state):
            return original_run_tool(
                task_id,
                name,
                args,
                git_token_value=git_token_value,
            )

        base = _base_policy(agent)
        unexpected = sorted(key for key in args if key != "note")
        note = str(args.get("note") or "").strip()
        fields, missing = _parse_hypothesis_note(base, note)
        if unexpected or missing:
            detail = ""
            if unexpected:
                detail += " Unexpected arguments: " + ", ".join(unexpected) + "."
            if missing:
                detail += " Missing or underspecified fields: " + ", ".join(missing) + "."
            return _contract_error(
                error=_ERROR_REQUIRED,
                message=(
                    "The forced-action hypothesis transition requires coding_update_plan to persist the "
                    "four-field hypothesis in the note argument. Assistant prose, goal/items updates, and "
                    "milestone summaries do not satisfy the contract."
                    + detail
                ),
                base=base,
                state=state,
                missing_fields=missing,
            )

        repository_evidence = str(fields.get("Repository evidence") or "")
        linked = _linked_targets(evidence_policy, repository_evidence, state)
        if not linked:
            return _contract_error(
                error=_ERROR_UNLINKED,
                message=(
                    "Repository evidence did not cite any exact verified causal target. Revise the note "
                    "to cite one of the verified repository-relative paths returned with this error."
                ),
                base=base,
                state=state,
            )

        canonical_args = {"note": _canonical_note(base, fields)}
        result = original_run_tool(
            task_id,
            name,
            canonical_args,
            git_token_value=git_token_value,
        )
        if not bool(result.get("ok")):
            return result

        after_task = agent.cw.load_task(task_id)
        after_state = agent.forced_action.active_state(after_task)
        persisted = bool(
            after_state.get("hypothesis_ready")
            and after_state.get("hypothesis_causal_evidence_linked")
        )
        if not persisted:
            failed = dict(result)
            failed.update(
                _contract_error(
                    error=_ERROR_NOT_PERSISTED,
                    message=(
                        "The project-plan write completed, but the controller could not re-read the durable "
                        "plan as a verified structured hypothesis. Revise coding_update_plan.note using the "
                        "exact labelled contract before attempting an edit."
                    ),
                    base=base,
                    state=after_state or state,
                    missing_fields=[
                        label
                        for label in base._HYPOTHESIS_FIELDS
                        if label not in set((after_state or {}).get("hypothesis_fields") or [])
                    ],
                )
            )
            return failed

        enriched = dict(result)
        enriched.update(
            {
                "hypothesis_persisted": True,
                "hypothesis_fields": [str(label) for label in base._HYPOTHESIS_FIELDS],
                "hypothesis_causal_targets": list(
                    after_state.get("hypothesis_causal_targets") or linked
                ),
                "next_action_kind": str(after_state.get("action_kind") or ""),
                "next_allowed_tools": sorted(after_state.get("allowed_tools") or []),
            }
        )
        return enriched

    agent._run_tool = run_tool_with_hypothesis_persistence
    # coding_agent_guarded intentionally exports its installed _run_tool seam and
    # historical qualification asserts identity with the agent hook. Preserve
    # that invariant while composing this wrapper around semantic acceptance.
    if (
        guarded_agent is not None
        and getattr(guarded_agent, "_run_tool_with_semantic_acceptance", None)
        is original_run_tool
    ):
        guarded_agent._run_tool_with_semantic_acceptance = run_tool_with_hypothesis_persistence
    agent._coding_hypothesis_persistence_installed = True
