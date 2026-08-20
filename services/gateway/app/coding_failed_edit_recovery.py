from __future__ import annotations

import re
from types import MethodType
from typing import Any, Dict, Mapping, Sequence


_CONTEXT_EDIT_TOOLS = {"coding_replace_text", "coding_apply_patch"}
_MUTATION_TOOLS = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}


def _normalized_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def _event_timestamp(event: Mapping[str, Any]) -> float:
    try:
        return max(0.0, float(event.get("ts") or 0))
    except (TypeError, ValueError):
        return 0.0


def _events(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in (task.get("agent_events") or [])
        if isinstance(item, Mapping)
    ]


def _successful_result(event: Mapping[str, Any]) -> Mapping[str, Any]:
    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    if result.get("ok") is False or str(result.get("error") or "").strip():
        return {}
    return result


def _latest_plan_index(events: Sequence[Mapping[str, Any]]) -> int:
    latest = -1
    for index, event in enumerate(events):
        if (
            str(event.get("type") or "") == "tool_finished"
            and str(event.get("name") or "") == "coding_update_plan"
            and _successful_result(event)
        ):
            latest = index
    return latest


def _plan_updated_at(task: Mapping[str, Any], state: Mapping[str, Any]) -> float:
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), Mapping) else {}
    values = []
    for value in (
        plan.get("updated_at"),
        state.get("durable_hypothesis_note_updated_at"),
    ):
        try:
            values.append(max(0.0, float(value or 0)))
        except (TypeError, ValueError):
            continue
    return max(values or [0.0])


def _matching_started_args(
    events: Sequence[Mapping[str, Any]],
    finish_index: int,
    finish: Mapping[str, Any],
) -> Dict[str, Any]:
    name = str(finish.get("name") or "").strip()
    call_id = str(finish.get("tool_call_id") or "").strip()
    cycle = finish.get("cycle")
    for event in reversed(events[:finish_index]):
        if str(event.get("type") or "") != "tool_started":
            continue
        if str(event.get("name") or "").strip() != name:
            continue
        event_call_id = str(event.get("tool_call_id") or "").strip()
        if call_id:
            if event_call_id != call_id:
                continue
        elif cycle not in (None, "") and event.get("cycle") != cycle:
            continue
        return dict(event.get("args")) if isinstance(event.get("args"), Mapping) else {}
    return {}


def _patch_paths(args: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    raw_paths = result.get("paths")
    paths: list[str] = []
    if isinstance(raw_paths, list):
        for item in raw_paths:
            path = _normalized_path(item)
            if path and path not in paths:
                paths.append(path)
    if paths:
        return paths

    patch = str(args.get("patch") or "")
    for match in re.finditer(r"^\+\+\+\s+(?:b/)?([^\t\r\n]+)$", patch, re.MULTILINE):
        path = _normalized_path(match.group(1))
        if path and path != "dev/null" and path not in paths:
            paths.append(path)
    return paths


def _edit_paths(name: str, args: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    if name in {"coding_write_file", "coding_replace_text"}:
        path = _normalized_path(args.get("path") or result.get("path"))
        return [path] if path else []
    if name == "coding_apply_patch":
        return _patch_paths(args, result)
    return []


def _refresh_span(state: Mapping[str, Any], path: str) -> tuple[int, int] | None:
    target = _normalized_path(path)
    if not target:
        return None
    for key in ("hypothesis_causal_evidence_ranges", "causal_evidence_ranges"):
        for item in state.get(key) or []:
            if not isinstance(item, Mapping) or _normalized_path(item.get("path")) != target:
                continue
            try:
                start = int(item.get("start_line"))
                end = int(item.get("end_line"))
            except (TypeError, ValueError):
                continue
            if start > 0 and end >= start:
                return start, end
    return None


def _failed_context_edit(
    agent: Any,
    events: Sequence[Mapping[str, Any]],
    index: int,
    event: Mapping[str, Any],
) -> dict[str, Any] | None:
    name = str(event.get("name") or "").strip()
    if str(event.get("type") or "") != "tool_finished" or name not in _CONTEXT_EDIT_TOOLS:
        return None

    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    args = _matching_started_args(events, index, event)
    predicate = getattr(agent, "_tool_result_modified_workspace", None)
    if callable(predicate):
        try:
            if bool(predicate(name, args, dict(result))):
                return None
        except Exception:
            pass

    error = str(result.get("error") or "").strip()
    if name == "coding_replace_text":
        path = _normalized_path(args.get("path") or result.get("path"))
        if not path:
            return None
        try:
            replacements = int(result.get("replacements") or 0)
        except (TypeError, ValueError):
            replacements = 0
        failed = result.get("ok") is False or bool(error) or replacements <= 0
        if not failed:
            return None
        return {
            "tool": name,
            "path": path,
            "error": error or "replacement made no workspace mutation",
            "event_index": index,
            "event_ts": _event_timestamp(event),
        }

    # A successful check-only patch is an intentional non-mutation and must not
    # trigger recovery. A failed patch application/check against one repository
    # path does need fresh exact context before another patch attempt.
    if result.get("ok") is not False and not error:
        return None
    paths = _patch_paths(args, result)
    if len(paths) != 1:
        return None
    return {
        "tool": name,
        "path": paths[0],
        "error": error or "patch did not apply",
        "event_index": index,
        "event_ts": _event_timestamp(event),
    }


def _successful_read_after(
    events: Sequence[Mapping[str, Any]],
    *,
    index: int,
    path: str,
    state: Mapping[str, Any],
) -> bool:
    expected_span = _refresh_span(state, path)
    for event in events[index + 1 :]:
        if (
            str(event.get("type") or "") != "tool_finished"
            or str(event.get("name") or "") != "coding_read_file_lines"
        ):
            continue
        result = _successful_result(event)
        if not result or _normalized_path(result.get("path")) != path or "content" not in result:
            continue
        if expected_span is None:
            return True
        try:
            actual_start = int(result.get("start_line"))
            actual_end = int(result.get("end_line"))
        except (TypeError, ValueError):
            continue
        expected_start, expected_end = expected_span
        if actual_start <= expected_start and actual_end >= expected_end:
            return True
    return False


def _successful_mutation_after(
    agent: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    index: int,
    path: str,
) -> bool:
    predicate = getattr(agent, "_tool_result_modified_workspace", None)
    if not callable(predicate):
        return False
    for later_index, event in enumerate(events[index + 1 :], start=index + 1):
        name = str(event.get("name") or "").strip()
        if str(event.get("type") or "") != "tool_finished" or name not in _MUTATION_TOOLS:
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        args = _matching_started_args(events, later_index, event)
        try:
            mutated = bool(predicate(name, args, dict(result)))
        except Exception:
            mutated = False
        if mutated and path in _edit_paths(name, args, result):
            return True
    return False


def _latest_failed_edit_needing_refresh(
    agent: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    events = _events(task)
    if not events:
        return None
    latest_plan = _latest_plan_index(events)
    plan_updated_at = _plan_updated_at(task, state)

    for index in range(len(events) - 1, -1, -1):
        if latest_plan >= 0 and index <= latest_plan:
            break
        attempt = _failed_context_edit(agent, events, index, events[index])
        if not attempt:
            continue
        if latest_plan < 0 and plan_updated_at > 0 and float(attempt["event_ts"] or 0) <= plan_updated_at:
            continue
        path = str(attempt["path"])
        if _successful_read_after(events, index=index, path=path, state=state):
            return None
        if _successful_mutation_after(agent, events, index=index, path=path):
            return None
        return attempt
    return None


def _clear_recovery_fields(out: Dict[str, Any]) -> None:
    for key in (
        "failed_edit_refresh_required",
        "failed_edit_refresh_target",
        "failed_edit_refresh_tool",
        "failed_edit_refresh_error",
        "failed_edit_refresh_event_index",
        "failed_edit_refresh_at",
        "failed_edit_refresh_start_line",
        "failed_edit_refresh_end_line",
        "failed_edit_refresh_line_count",
    ):
        out.pop(key, None)


def refine_state(
    agent: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Force a current-source refresh after a stale exact edit fails.

    The controller may intentionally remove inspection tools once a structured
    hypothesis is provenance-qualified. If an exact replace/patch then fails
    because its source context no longer matches the workspace, repeatedly
    retrying the same edit cannot make progress. Temporarily authorize one
    bounded read of that exact target. When range-qualified causal evidence is
    available, re-read that same span rather than merely locking the file path.
    The existing evidence-freshness overlay then requires a new hypothesis if
    the refreshed source postdates linked causal evidence.
    """
    out = dict(state)
    if str(out.get("action_kind") or "") != "edit":
        return out

    attempt = _latest_failed_edit_needing_refresh(agent, task, out)
    if not attempt:
        _clear_recovery_fields(out)
        return out

    target = str(attempt["path"])
    span = _refresh_span(out, target)
    out["action_kind"] = "evidence"
    out["allowed_tools"] = ["coding_finish", "coding_read_file_lines"]
    out["failed_edit_refresh_required"] = True
    out["failed_edit_refresh_target"] = target
    out["failed_edit_refresh_tool"] = str(attempt["tool"])
    out["failed_edit_refresh_error"] = str(attempt["error"])[:500]
    out["failed_edit_refresh_event_index"] = int(attempt["event_index"])
    out["failed_edit_refresh_at"] = float(attempt["event_ts"] or 0)
    if span is not None:
        start, end = span
        out["failed_edit_refresh_start_line"] = start
        out["failed_edit_refresh_end_line"] = end
        out["failed_edit_refresh_line_count"] = end - start + 1
        read_instruction = (
            f"Use coding_read_file_lines exactly on {target} with start_line={start} and "
            f"line_count={end - start + 1} to refresh the same verified causal span."
        )
    else:
        read_instruction = (
            f"Use one bounded coding_read_file_lines call on exactly {target} to refresh current source."
        )
    out["required_action"] = (
        f"The previous {attempt['tool']} attempt did not modify the workspace because its exact "
        f"source context failed ({attempt['error']}). Do not retry that edit from stale replay. "
        f"{read_instruction} After that refresh, follow the evidence-freshness policy before editing again."
    )
    return out


def install(agent: Any, evidence_policy: Any) -> None:
    """Install failed-edit recovery hooks around provenance and runtime authorization."""
    if bool(getattr(evidence_policy, "_coding_failed_edit_recovery_installed", False)):
        return

    # Keep an inner refinement so request materialization paths that explicitly
    # call evidence_policy.apply_provenance_gate observe the recovery state. The
    # final execution-state wrapper installed after durable hypothesis
    # persistence reapplies this refinement authoritatively.
    original_apply = evidence_policy.apply_provenance_gate

    def apply_with_failed_edit_recovery(
        forced_action: Any,
        task: Mapping[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        effective = original_apply(forced_action, task, state)
        return refine_state(agent, task, effective)

    evidence_policy.apply_provenance_gate = apply_with_failed_edit_recovery

    original_prompt = evidence_policy._provenance_prompt_context

    def prompt_with_failed_edit_recovery(base: Any, state: Mapping[str, Any]) -> str:
        if not state.get("failed_edit_refresh_required"):
            return original_prompt(base, state)
        target = str(state.get("failed_edit_refresh_target") or "").strip()
        tool = str(state.get("failed_edit_refresh_tool") or "edit").strip()
        error = str(state.get("failed_edit_refresh_error") or "exact source context did not match").strip()
        start = state.get("failed_edit_refresh_start_line")
        count = state.get("failed_edit_refresh_line_count")
        if start and count:
            refresh = f"Call coding_read_file_lines exactly on {target} with start_line={start} and line_count={count}."
        else:
            refresh = f"Call coding_read_file_lines exactly once on {target} to refresh current source."
        return (
            "Controller forced-action mode is ACTIVE. The previous exact edit did not mutate the "
            f"workspace: {tool} failed with {error}. Do not retry the stale edit and do not search "
            f"elsewhere. {refresh} After the read, obey the next controller policy; newer causal "
            "evidence may require revising the structured hypothesis before editing. coding_finish "
            "remains available for a concrete blocker."
        )

    evidence_policy._provenance_prompt_context = prompt_with_failed_edit_recovery

    forced_action = agent.forced_action
    original_evaluate = forced_action.evaluate_tool_call

    def evaluate_with_refresh_target_lock(
        self: Any,
        task: Mapping[str, Any],
        *,
        name: str,
        args: Mapping[str, Any],
        is_validation_command: Any,
    ) -> tuple[bool, Dict[str, Any]]:
        state = self.active_state(task)
        if state.get("failed_edit_refresh_required") and str(name or "").strip() == "coding_read_file_lines":
            expected = _normalized_path(state.get("failed_edit_refresh_target"))
            actual = _normalized_path(args.get("path"))
            required = str(state.get("required_action") or "").strip()
            if expected and actual != expected:
                return False, {
                    "ok": False,
                    "error": "failed_edit_refresh_target_mismatch",
                    "message": (
                        f"Failed-edit recovery permits a bounded read only of {expected}; "
                        f"the attempted path was {actual or '(missing)'}."
                    ),
                    "required_action": required,
                    "allowed_tools": list(state.get("allowed_tools") or []),
                    "action_kind": state.get("action_kind"),
                    "canonical_action_kind": state.get("canonical_action_kind"),
                    "failed_edit_refresh_target": expected,
                }
            expected_start = state.get("failed_edit_refresh_start_line")
            expected_count = state.get("failed_edit_refresh_line_count")
            if expected_start and expected_count:
                try:
                    actual_start = int(args.get("start_line"))
                    actual_count = int(args.get("line_count"))
                except (TypeError, ValueError):
                    actual_start = 0
                    actual_count = 0
                if actual_start != int(expected_start) or actual_count != int(expected_count):
                    return False, {
                        "ok": False,
                        "error": "failed_edit_refresh_range_mismatch",
                        "message": (
                            "Failed-edit recovery must refresh the same verified causal span: "
                            f"{expected}:{expected_start}-"
                            f"{int(expected_start) + int(expected_count) - 1}."
                        ),
                        "required_action": required,
                        "allowed_tools": list(state.get("allowed_tools") or []),
                        "action_kind": state.get("action_kind"),
                        "canonical_action_kind": state.get("canonical_action_kind"),
                        "failed_edit_refresh_target": expected,
                        "failed_edit_refresh_start_line": int(expected_start),
                        "failed_edit_refresh_line_count": int(expected_count),
                    }
        return original_evaluate(
            task,
            name=name,
            args=args,
            is_validation_command=is_validation_command,
        )

    forced_action.evaluate_tool_call = MethodType(
        evaluate_with_refresh_target_lock,
        forced_action,
    )
    evidence_policy._coding_failed_edit_recovery_installed = True
