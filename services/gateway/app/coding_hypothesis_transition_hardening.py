from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from app import coding_evidence_range_provenance as evidence_range_provenance
from app import coding_hypothesis_range_contract as hypothesis_range_contract


_CONTRACT_ERRORS = {
    "coding_update_plan_hypothesis_required",
    "coding_update_plan_hypothesis_unlinked",
    "coding_update_plan_hypothesis_range_unverified",
}
_SAFE_RESULT_KEYS = (
    "ok",
    "error",
    "message",
    "required_action",
    "missing_hypothesis_fields",
    "required_hypothesis_fields",
    "verified_causal_targets",
    "verified_causal_ranges",
    "unverified_bounded_targets",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tool_name(spec: Any) -> str:
    function = getattr(spec, "function", None)
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(getattr(function, "name", "") or "").strip()


def _contract_preflight(
    agent: Any,
    forced_action: Any,
    hypothesis_persistence: Any,
    evidence_policy: Any,
    task: Mapping[str, Any],
    args: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    state = forced_action.active_state(task)
    if not hypothesis_persistence._contract_required(state):
        return None

    base = hypothesis_persistence._base_policy(agent)
    unexpected = sorted(str(key) for key in args if str(key) != "note")
    note = str(args.get("note") or "").strip()
    fields, missing = hypothesis_persistence._parse_hypothesis_note(base, note)
    if unexpected or missing:
        detail = ""
        if unexpected:
            detail += " Unexpected arguments: " + ", ".join(unexpected) + "."
        if missing:
            detail += " Missing or underspecified fields: " + ", ".join(missing) + "."
        return hypothesis_persistence._contract_error(
            error=getattr(
                hypothesis_persistence,
                "_ERROR_REQUIRED",
                "coding_update_plan_hypothesis_required",
            ),
            message=(
                "The forced-action hypothesis transition requires coding_update_plan to persist the "
                "four-field hypothesis in the note argument. Assistant prose, goal/items updates, and "
                "milestone summaries do not satisfy the contract." + detail
            ),
            base=base,
            state=state,
            missing_fields=missing,
        )

    canonical_note = hypothesis_persistence._canonical_note(base, fields)
    note_limit = int(getattr(hypothesis_persistence, "_PLAN_NOTE_MAX_CHARS", 2000) or 2000)
    if len(canonical_note) > note_limit:
        return hypothesis_persistence._contract_error(
            error=getattr(
                hypothesis_persistence,
                "_ERROR_REQUIRED",
                "coding_update_plan_hypothesis_required",
            ),
            message=(
                f"The complete structured hypothesis must fit within {note_limit} characters. "
                "Shorten the findings without dropping any required labelled field."
            ),
            base=base,
            state=state,
        )

    repository_evidence = str(fields.get("Repository evidence") or "")
    linked = hypothesis_persistence._linked_targets(
        evidence_policy,
        repository_evidence,
        state,
    )
    if not linked:
        return hypothesis_persistence._contract_error(
            error=getattr(
                hypothesis_persistence,
                "_ERROR_UNLINKED",
                "coding_update_plan_hypothesis_unlinked",
            ),
            message=(
                "Repository evidence did not cite an exact verified causal target. Revise the note "
                "to cite one of the verified repository-relative paths returned with this error."
            ),
            base=base,
            state=state,
        )

    # Keep the policy preflight equivalent to the later execution wrapper. When
    # bounded evidence exists, a path-only citation is not enough: each bounded
    # target named in Repository evidence must cite a span contained in the
    # actual completed read. Handling this here makes range mistakes participate
    # in the normal forced-action rejection counter instead of becoming a second
    # post-policy no-progress loop.
    if hypothesis_range_contract._verified_ranges(state):
        validation = evidence_range_provenance.validate_repository_evidence(
            evidence_policy,
            forced_action,
            task,
            state,
            repository_evidence,
            targets=linked,
        )
        if (
            validation.get("missing_range_targets")
            or not validation.get("matched_targets")
        ):
            return hypothesis_range_contract._range_error(
                hypothesis_persistence,
                agent,
                state,
                validation,
            )
    return None


def _safe_tool_result(debug_report: Any, result: Mapping[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key in _SAFE_RESULT_KEYS:
        if key not in result:
            continue
        value = result.get(key)
        if isinstance(value, str):
            safe[key] = debug_report.redact_text(value, limit=1200)
        elif isinstance(value, (list, tuple)):
            safe[key] = [
                debug_report.redact_text(item, limit=300) if isinstance(item, str) else item
                for item in list(value)[:20]
            ]
        elif value is None or isinstance(value, (bool, int, float)):
            safe[key] = value
    return safe


def _progress_for_forced_action(
    snapshot: Dict[str, Any],
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    if not state:
        return snapshot
    plan = _mapping(task.get("project_plan"))
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    if items:
        return snapshot
    changes = _mapping(snapshot.get("changes"))
    counts = _mapping(changes.get("counts"))
    changed_total = int(counts.get("total") or len(changes.get("changed_files") or []))
    if changed_total:
        return snapshot
    required = str(state.get("required_action") or "").strip()
    if not required:
        return snapshot

    progress = dict(_mapping(snapshot.get("progress")))
    kind = str(state.get("action_kind") or "").strip()
    if kind == "diff_review":
        progress["current_phase"] = "reviewing"
    elif kind == "finish":
        progress["current_phase"] = "finalizing"
    else:
        progress["current_phase"] = "editing"
    progress["next_recommended_action"] = required
    snapshot["progress"] = progress
    return snapshot


def install(
    agent: Any,
    cw: Any,
    forced_action: Any,
    hypothesis_persistence: Any,
    evidence_policy: Any,
    debug_report: Any,
) -> None:
    """Close hypothesis-transition composition, enforcement, and observability gaps."""
    if bool(getattr(agent, "_coding_hypothesis_transition_hardening_installed", False)):
        return

    # Preserve every previously installed task-specific transformation. Some
    # earlier resolvers captured the pre-mission raw tool set, so merge only
    # names that are newly available from the current raw set (notably
    # coding_refute_hypothesis), then run the current forced-action filter. This
    # keeps specialized specs already produced by earlier overlays instead of
    # replacing them with generic raw definitions.
    prior_specs_for_task = agent._tool_specs_for_task

    def specs_for_task_with_contract_guard(task: Dict[str, Any]) -> list[Any]:
        specs = list(prior_specs_for_task(task))
        names = {_tool_name(spec) for spec in specs if _tool_name(spec)}
        raw_specs = getattr(agent, "_tool_specs", None)
        if callable(raw_specs):
            try:
                for spec in raw_specs():
                    name = _tool_name(spec)
                    if name and name not in names:
                        specs.append(spec)
                        names.add(name)
            except Exception:
                pass
        filter_specs = getattr(forced_action, "filter_tool_specs", None)
        if callable(filter_specs):
            specs = list(filter_specs(specs, task))

        state = forced_action.active_state(task)
        if not hypothesis_persistence._contract_required(state):
            return specs

        # Re-materialize the strict note-only persistence schema, then reapply
        # the bounded-range augmentation. The range layer runs before this late
        # overlay in production, so rebuilding only the base contract here would
        # otherwise erase its path:start-end guidance.
        out: list[Any] = []
        for spec in specs:
            specialized = hypothesis_persistence._contract_tool_spec(
                agent,
                spec,
                state,
            )
            specialized = hypothesis_range_contract._augment_tool_spec(
                agent,
                hypothesis_persistence,
                specialized,
                state,
            )
            out.append(specialized)
        return out

    agent._tool_specs_for_task = specs_for_task_with_contract_guard
    agent._tool_specs_for_task_before_hypothesis_transition_hardening = prior_specs_for_task

    # Validate the hypothesis transition before execution so malformed calls use
    # the existing forced-action rejection counter. Two unchanged violations now
    # trigger the established backend reroute/noncompliance path instead of
    # consuming the general no-progress window as apparently successful plan calls.
    prior_evaluate = forced_action.evaluate_tool_call

    def evaluate_tool_call_with_hypothesis_contract(
        task: Mapping[str, Any],
        *,
        name: str,
        args: Mapping[str, Any],
        is_validation_command: Any,
    ) -> tuple[bool, Dict[str, Any]]:
        allowed, rejection = prior_evaluate(
            task,
            name=name,
            args=args,
            is_validation_command=is_validation_command,
        )
        if not allowed or str(name or "") != "coding_update_plan":
            return allowed, rejection
        contract_rejection = _contract_preflight(
            agent,
            forced_action,
            hypothesis_persistence,
            evidence_policy,
            task,
            args,
        )
        if contract_rejection is None:
            return True, {}
        contract_rejection = dict(contract_rejection)
        contract_rejection["policy_contract_rejection"] = True
        contract_rejection["attempted_tool"] = "coding_update_plan"
        return False, contract_rejection

    forced_action.evaluate_tool_call = evaluate_tool_call_with_hypothesis_contract

    # Preserve safe result diagnostics in debug artifacts. The raw note/content
    # remains redacted; only bounded controller error metadata is exposed.
    prior_event_view = debug_report._event_view

    def event_view_with_tool_result(event: Dict[str, Any]) -> Dict[str, Any]:
        output = dict(prior_event_view(event))
        if str(event.get("type") or "") != "tool_finished":
            return output
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        safe = _safe_tool_result(debug_report, result)
        if safe:
            output["result"] = debug_report._sanitize(safe)
        return output

    debug_report._event_view = event_view_with_tool_result

    # Mission acceptance is already present in the durable coding snapshot, but
    # the debug serializer historically dropped unknown sections. Keep this one
    # explicitly because it is part of the acceptance invariant under test.
    prior_durable_state_view = debug_report._durable_state_view

    def durable_state_view_with_mission_acceptance(result: Dict[str, Any]) -> Dict[str, Any]:
        output = dict(prior_durable_state_view(result))
        if not result.get("ok"):
            return output
        state = result.get("value") if isinstance(result.get("value"), Mapping) else {}
        mission_acceptance = state.get("mission_acceptance")
        if isinstance(mission_acceptance, Mapping):
            output["mission_acceptance"] = debug_report._sanitize(dict(mission_acceptance))
        return output

    debug_report._durable_state_view = durable_state_view_with_mission_acceptance

    # When a forced hypothesis transition is active and the project plan has no
    # milestones, do not fall back to the generic "continue current milestone"
    # recommendation. Surface the controller's exact required action instead.
    prior_snapshot = cw.coding_state_snapshot

    def coding_state_snapshot_with_forced_progress(task_id: str) -> Dict[str, Any]:
        snapshot = dict(prior_snapshot(task_id))
        try:
            task = cw.load_task(task_id)
            state = forced_action.active_state(task)
        except Exception:
            return snapshot
        return _progress_for_forced_action(snapshot, task, state)

    cw.coding_state_snapshot = coding_state_snapshot_with_forced_progress
    agent._coding_hypothesis_transition_hardening_installed = True


def is_contract_error(result: Mapping[str, Any]) -> bool:
    """Return whether a result is one of the model-correctable hypothesis errors."""
    return str(result.get("error") or "") in _CONTRACT_ERRORS
