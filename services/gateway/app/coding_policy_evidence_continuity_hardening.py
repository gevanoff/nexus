from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence


_REFUTATION_TOOL = "coding_refute_hypothesis"
_POLICY_MISMATCH = "coding_execution_policy_contract_mismatch"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tool_name(spec: Any) -> str:
    function = getattr(spec, "function", None)
    if function is None and isinstance(spec, Mapping):
        function = spec.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(getattr(function, "name", "") or "").strip()


def _raw_tool_names(agent: Any) -> set[str]:
    getter = getattr(agent, "_tool_specs", None)
    if not callable(getter):
        return set()
    try:
        return {
            name
            for name in (_tool_name(spec) for spec in getter())
            if name
        }
    except Exception:
        return set()


def _canonical_edit_prompt(state: Mapping[str, Any]) -> str:
    allowed = sorted(
        {
            str(item).strip()
            for item in (state.get("allowed_tools") or [])
            if str(item).strip()
        }
    )
    required = str(state.get("required_action") or "").strip() or (
        "Make the smallest evidence-backed edit, or finish with a concrete blocker."
    )
    rendered = (
        "Controller forced-action mode is ACTIVE. The effective controller state for this turn is edit. "
        "Verified causal repository evidence is already linked to the durable remediation hypothesis; "
        "inspection and generic plan churn are disabled. "
        f"Required action: {required} Available tools: {', '.join(allowed) or 'coding_finish'}."
    )
    if _REFUTATION_TOOL in allowed:
        rendered += (
            " If the replayed verified repository evidence contradicts the current remediation hypothesis, "
            "call coding_refute_hypothesis instead of making an edit based on the contradicted hypothesis."
        )
    return rendered


def _install_live_policy_view(agent: Any, policy: Any) -> None:
    if bool(getattr(policy, "_coding_live_policy_view_installed", False)):
        return
    active_state = getattr(policy, "active_state", None)
    if not callable(active_state):
        return

    raw_names = _raw_tool_names(agent)
    prior_active_state = active_state

    def active_state_with_edit_extensions(task: Mapping[str, Any]) -> Dict[str, Any]:
        state = dict(prior_active_state(task) or {})
        if (
            state
            and str(state.get("action_kind") or "") == "edit"
            and _REFUTATION_TOOL in raw_names
        ):
            allowed = {
                str(item).strip()
                for item in (state.get("allowed_tools") or [])
                if str(item).strip()
            }
            allowed.add(_REFUTATION_TOOL)
            state["allowed_tools"] = sorted(allowed)
        return state

    policy.active_state = active_state_with_edit_extensions
    policy._coding_live_policy_active_state_before_hardening = prior_active_state

    prior_prompt = getattr(policy, "prompt_context", None)
    if callable(prior_prompt):
        def prompt_context_with_canonical_edit(task: Mapping[str, Any]) -> str:
            state = policy.active_state(task)
            if state and str(state.get("action_kind") or "") == "edit":
                return _canonical_edit_prompt(state)
            return str(prior_prompt(task) or "")

        policy.prompt_context = prompt_context_with_canonical_edit
        policy._coding_live_policy_prompt_before_hardening = prior_prompt

    policy._coding_live_policy_view_installed = True


def _span(result: Mapping[str, Any]) -> tuple[int, int] | None:
    try:
        start = int(result.get("start_line"))
        end = int(result.get("end_line"))
    except (TypeError, ValueError):
        return None
    if start <= 0 or end < start:
        return None
    return start, end


def _desired_ranges(
    persistence: Any,
    state: Mapping[str, Any],
    targets: Sequence[str],
) -> dict[str, list[tuple[int, int]]]:
    wanted: dict[str, list[tuple[int, int]]] = defaultdict(list)
    target_set = {
        persistence._normalized_path(item)
        for item in targets
        if persistence._normalized_path(item)
    }
    for raw in state.get("causal_evidence_ranges") or []:
        item = _mapping(raw)
        path = persistence._normalized_path(item.get("path"))
        if path not in target_set:
            continue
        try:
            start = int(item.get("start_line"))
            end = int(item.get("end_line"))
        except (TypeError, ValueError):
            continue
        if start <= 0 or end < start:
            continue
        pair = (start, end)
        if pair not in wanted[path]:
            wanted[path].append(pair)
    for values in wanted.values():
        values.sort()
    return dict(wanted)


def multi_range_verified_evidence_bundle(
    persistence: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Replay every verified causal read range, not merely the latest read per path.

    A later bounded read of one portion of a file must not erase an earlier
    contradictory causal range from the next hypothesis/edit turn. Legacy events
    without line metadata retain the established one-excerpt-per-path behavior.
    """
    from app import coding_edit_evidence_continuity as continuity

    targets = continuity._ordered_targets(state, persistence)
    if not targets:
        return "", []

    desired = _desired_ranges(persistence, state, targets)
    exact: dict[tuple[str, int, int], str] = {}
    latest_by_path: dict[str, tuple[str, tuple[int, int] | None]] = {}
    for raw in task.get("agent_events") or []:
        event = _mapping(raw)
        if (
            str(event.get("type") or "") != "tool_finished"
            or str(event.get("name") or "") != "coding_read_file_lines"
        ):
            continue
        result = persistence._successful_event_result(event)
        path = persistence._normalized_path(result.get("path"))
        content = result.get("content")
        if path not in targets or not isinstance(content, str) or not content.strip():
            continue
        span = _span(result)
        latest_by_path[path] = (content, span)
        if span is not None:
            exact[(path, span[0], span[1])] = content

    selected: list[dict[str, Any]] = []
    for path in targets:
        ranges = desired.get(path) or []
        if ranges:
            for start, end in ranges:
                content = exact.get((path, start, end))
                if content is None:
                    continue
                selected.append(
                    {
                        "path": path,
                        "start_line": start,
                        "end_line": end,
                        "content": content,
                    }
                )
            continue
        fallback = latest_by_path.get(path)
        if fallback is None:
            continue
        content, span = fallback
        item: dict[str, Any] = {"path": path, "content": content}
        if span is not None:
            item["start_line"], item["end_line"] = span
        selected.append(item)

    if not selected:
        return "", []

    counts_by_path: dict[str, int] = defaultdict(int)
    for item in selected:
        counts_by_path[str(item["path"])] += 1
    total_blocks = len(selected)
    global_share = max(128, continuity._MAX_TOTAL_CHARS // max(1, total_blocks))

    blocks: list[str] = []
    metadata: list[dict[str, Any]] = []
    total = 0
    for item in selected:
        path = str(item["path"])
        start = item.get("start_line")
        end = item.get("end_line")
        locator = f"{path}:{start}-{end}" if start and end else path
        header = f"Repository path: {locator}\n"
        per_path_share = max(
            128,
            continuity._MAX_PATH_CHARS // max(1, counts_by_path[path]),
        )
        remaining = continuity._MAX_TOTAL_CHARS - total
        if remaining <= len(header) + 64:
            break
        limit = min(per_path_share, global_share, max(64, remaining - len(header)))
        content = str(item.get("content") or "")
        excerpt, clipped = continuity._line_aware_clip(content, limit)
        block = f"{header}{excerpt}"
        blocks.append(block)
        total += len(block)
        row: dict[str, Any] = {
            "path": path,
            "source_chars": len(content),
            "replayed_chars": len(excerpt),
            "clipped": clipped,
        }
        if start and end:
            row["start_line"] = int(start)
            row["end_line"] = int(end)
        metadata.append(row)

    return "\n\n".join(blocks), metadata


def _install_multi_range_replay() -> None:
    from app import coding_edit_evidence_continuity as continuity

    if bool(getattr(continuity, "_coding_multi_range_evidence_replay_installed", False)):
        return
    continuity._verified_evidence_bundle_before_multi_range = continuity.verified_evidence_bundle
    continuity.verified_evidence_bundle = multi_range_verified_evidence_bundle
    continuity._coding_multi_range_evidence_replay_installed = True


def _request_value(dispatch: Any, req: Any, name: str, default: Any = None) -> Any:
    getter = getattr(dispatch, "_request_value", None)
    if callable(getter):
        return getter(req, name, default)
    if isinstance(req, Mapping):
        return req.get(name, default)
    return getattr(req, name, default)


def assert_execution_policy_consistency(
    agent: Any,
    dispatch: Any,
    task: Mapping[str, Any],
    materialized: Any,
    snapshot: Any,
    diagnostics: Mapping[str, Any],
) -> None:
    if not bool(diagnostics.get("coding_request")):
        return
    effective_task = dispatch.coding_execution_policy.execution_task(agent, task)
    state = agent.forced_action.active_state(effective_task)
    if not state or "allowed_tools" not in state:
        return

    expected = tuple(
        sorted(
            {
                str(item).strip()
                for item in (state.get("allowed_tools") or [])
                if str(item).strip()
            }
        )
    )
    snapshot_allowed = tuple(sorted(str(item) for item in (snapshot.allowed_tools or ())))
    actual_specs = list(_request_value(dispatch, materialized, "tools", None) or [])
    actual = tuple(
        sorted(name for name in (_tool_name(spec) for spec in actual_specs) if name)
    )
    text_tool_mode = bool(getattr(snapshot, "text_tool_mode", False))

    mismatch = snapshot_allowed != expected
    if not text_tool_mode:
        mismatch = mismatch or actual != snapshot_allowed
    if not mismatch:
        return

    raise RuntimeError(
        f"{_POLICY_MISMATCH}: effective_action={state.get('action_kind')!s}; "
        f"effective_allowed={list(expected)!r}; snapshot_allowed={list(snapshot_allowed)!r}; "
        f"advertised_tools={list(actual)!r}; text_tool_mode={text_tool_mode}. "
        "Nexus refused to dispatch a Coding Workspace request with contradictory controller policy."
    )


def _install_dispatch_invariant() -> None:
    from app import coding_execution_dispatch as dispatch

    if bool(getattr(dispatch, "_coding_execution_policy_consistency_installed", False)):
        return
    prior_materialize = dispatch.materialize_request

    def materialize_with_policy_consistency(
        agent: Any,
        req: Any,
        task: Mapping[str, Any],
        *,
        source_backend: str,
        backend: str,
        upstream_model: str,
    ):
        materialized, snapshot, diagnostics = prior_materialize(
            agent,
            req,
            task,
            source_backend=source_backend,
            backend=backend,
            upstream_model=upstream_model,
        )
        assert_execution_policy_consistency(
            agent,
            dispatch,
            task,
            materialized,
            snapshot,
            diagnostics,
        )
        return materialized, snapshot, diagnostics

    dispatch.materialize_request = materialize_with_policy_consistency
    dispatch._materialize_request_before_policy_consistency = prior_materialize
    dispatch._coding_execution_policy_consistency_installed = True


def install(
    agent: Any,
    policy: Any,
) -> None:
    """Keep live controller policy, advertised tools, and evidence replay coherent."""
    _install_live_policy_view(agent, policy)
    _install_multi_range_replay()
    _install_dispatch_invariant()
