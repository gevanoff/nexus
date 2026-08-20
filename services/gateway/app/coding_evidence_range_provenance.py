from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Sequence


def _normalized_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def _events(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in (task.get("agent_events") or [])
        if isinstance(item, Mapping)
    ]


def _successful_read(event: Mapping[str, Any]) -> Mapping[str, Any]:
    if (
        str(event.get("type") or "") != "tool_finished"
        or str(event.get("name") or "") != "coding_read_file_lines"
    ):
        return {}
    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    if result.get("ok") is False or str(result.get("error") or "").strip():
        return {}
    if not _normalized_path(result.get("path")) or "content" not in result:
        return {}
    return result


def _line_range(result: Mapping[str, Any]) -> tuple[int, int] | None:
    try:
        start = int(result.get("start_line"))
        end = int(result.get("end_line"))
    except (TypeError, ValueError):
        return None
    if start <= 0 or end < start:
        return None
    return start, end


def _causal_targets(state: Mapping[str, Any]) -> set[str]:
    return {
        _normalized_path(item)
        for item in (state.get("causal_evidence_targets") or [])
        if _normalized_path(item)
    }


def _verified_ranges(
    evidence_policy: Any,
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[dict[str, list[tuple[int, int]]], set[str]]:
    events = _events(task)
    try:
        window_start = int(evidence_policy._evidence_window_start(events, state))
    except Exception:
        window_start = 0
    window_start = max(0, min(window_start, len(events)))
    causal = _causal_targets(state)
    if not causal:
        return {}, set()

    ranges: dict[str, list[tuple[int, int]]] = {}
    legacy_paths: set[str] = set()
    for event in events[window_start:]:
        result = _successful_read(event)
        if not result:
            continue
        path = _normalized_path(result.get("path"))
        # Only expose ranges the existing provenance gate has independently
        # classified as verified causal evidence. Acceptance fixtures, docs,
        # and incidental context reads must never appear as root-cause spans.
        if not path or path not in causal:
            continue
        span = _line_range(result)
        if span is None:
            legacy_paths.add(path)
            continue
        bucket = ranges.setdefault(path, [])
        if span not in bucket:
            bucket.append(span)
    for bucket in ranges.values():
        bucket.sort()
    return ranges, legacy_paths


def _hypothesis_note_fields(base: Any, note: str) -> Dict[str, str]:
    labels = tuple(str(label) for label in getattr(base, "_HYPOTHESIS_FIELDS", ()))
    field_re = getattr(base, "_HYPOTHESIS_FIELD_RE", None)
    text = str(note or "").strip()
    if not labels or field_re is None or not text:
        return {}

    matches = list(field_re.finditer(text))
    fields: Dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = str(match.group(1) or "").strip()
        label = next(
            (
                candidate
                for candidate in labels
                if candidate.casefold() == raw_label.casefold()
            ),
            "",
        )
        if not label or label in fields:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip(" \t\r\n;.")
        if len(value) >= 8:
            fields[label] = value

    if any(label not in fields for label in labels):
        return {}
    return fields


def _repository_evidence(forced_action: Any, task: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    """Read evidence only from the current durable hypothesis note.

    The general structured-hypothesis parser also scans goal/items for legacy
    compatibility. Those fields may retain an older four-field hypothesis after
    project_plan.note has been reconciled, so they cannot be authoritative for
    range qualification. The persistence layer owns note freshness/revision
    validation; when it has explicitly rejected the durable note, do not accept
    any range citation from it here.
    """
    if "durable_hypothesis_note_ready" in state and not bool(
        state.get("durable_hypothesis_note_ready")
    ):
        return ""

    base = getattr(forced_action, "_execution_provenance_base", forced_action)
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), Mapping) else {}
    fields = _hypothesis_note_fields(base, str(plan.get("note") or ""))
    return str(fields.get("Repository evidence") or "")


def _cited_ranges_for_path(repository_evidence: str, path: str) -> list[tuple[int, int]]:
    evidence = str(repository_evidence or "").replace("\\", "/")
    normalized = _normalized_path(path)
    if not evidence or not normalized:
        return []

    pattern = re.compile(re.escape(normalized), re.IGNORECASE)
    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(evidence):
        tail = evidence[match.end() : match.end() + 120]
        forms = (
            r"^\s*#L(\d+)(?:\s*-\s*L?(\d+))?",
            r"^\s*:(\d+)(?:\s*-\s*(\d+))?",
            r"^\s*(?:,\s*)?lines?\s+(\d+)(?:\s*[-–]\s*(\d+))?",
        )
        for form in forms:
            found = re.search(form, tail, re.IGNORECASE)
            if not found:
                continue
            start = int(found.group(1))
            end = int(found.group(2) or start)
            if start > 0 and end >= start:
                span = (start, end)
                if span not in spans:
                    spans.append(span)
            break
    return spans


def _span_is_verified(cited: tuple[int, int], verified: Sequence[tuple[int, int]]) -> bool:
    cited_start, cited_end = cited
    return any(
        cited_start >= verified_start and cited_end <= verified_end
        for verified_start, verified_end in verified
    )


def _range_metadata(ranges: Mapping[str, Sequence[tuple[int, int]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(ranges):
        for start, end in ranges[path]:
            out.append({"path": path, "start_line": start, "end_line": end})
    return out


def validate_repository_evidence(
    evidence_policy: Any,
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
    repository_evidence: str,
    *,
    targets: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Validate cited spans against the actual completed bounded reads.

    This is the single range validator used by both the durable-hypothesis
    preflight contract and the final execution-state gate. Requested read bounds
    are never authoritative; only ``tool_finished.result.start_line/end_line``
    are accepted, so EOF-shortened reads cannot accidentally authorize a wider
    citation.
    """
    ranges, legacy_paths = _verified_ranges(evidence_policy, forced_action, task, state)
    candidate_targets = [
        _normalized_path(item)
        for item in (targets if targets is not None else state.get("hypothesis_causal_targets") or [])
        if _normalized_path(item)
    ]
    matched_targets: list[str] = []
    matched_ranges: list[dict[str, Any]] = []
    missing_range_targets: list[str] = []

    for target in candidate_targets:
        verified = ranges.get(target) or []
        if not verified:
            # Old durable events and synthetic fixtures may not carry line
            # metadata. Preserve their established path-only semantics.
            if target in legacy_paths or target not in ranges:
                matched_targets.append(target)
            continue
        cited = _cited_ranges_for_path(repository_evidence, target)
        verified_citations = [span for span in cited if _span_is_verified(span, verified)]
        if verified_citations:
            matched_targets.append(target)
            for start, end in verified_citations:
                matched_ranges.append(
                    {"path": target, "start_line": start, "end_line": end}
                )
        else:
            missing_range_targets.append(target)

    return {
        "ok": bool(matched_targets) or not missing_range_targets,
        "matched_targets": matched_targets,
        "matched_ranges": matched_ranges,
        "missing_range_targets": missing_range_targets,
        "verified_ranges": _range_metadata(ranges),
        "legacy_paths": sorted(legacy_paths),
    }


def _clear_range_requirement(out: Dict[str, Any]) -> None:
    out.pop("hypothesis_evidence_range_required", None)
    out.pop("hypothesis_evidence_range_targets", None)


def refine_state(
    evidence_policy: Any,
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Require modern bounded reads to be cited at their verified line range."""
    out = dict(state)
    ranges, _ = _verified_ranges(evidence_policy, forced_action, task, out)
    if ranges:
        out["causal_evidence_ranges"] = _range_metadata(ranges)
    else:
        out.pop("causal_evidence_ranges", None)

    if str(out.get("action_kind") or "") != "edit":
        return out
    if not out.get("hypothesis_causal_evidence_linked"):
        return out

    linked_targets = [
        _normalized_path(item)
        for item in (out.get("hypothesis_causal_targets") or [])
        if _normalized_path(item)
    ]
    if not linked_targets:
        return out

    repository_evidence = _repository_evidence(forced_action, task, out)
    validation = validate_repository_evidence(
        evidence_policy,
        forced_action,
        task,
        out,
        repository_evidence,
        targets=linked_targets,
    )
    matched_targets = list(validation.get("matched_targets") or [])
    matched_ranges = list(validation.get("matched_ranges") or [])
    missing_range_targets = list(validation.get("missing_range_targets") or [])

    if matched_targets:
        _clear_range_requirement(out)
        out["hypothesis_causal_targets"] = matched_targets
        out["hypothesis_causal_evidence_linked"] = True
        if matched_ranges:
            out["hypothesis_causal_evidence_ranges"] = matched_ranges
        else:
            out.pop("hypothesis_causal_evidence_ranges", None)
        return out

    if not missing_range_targets:
        _clear_range_requirement(out)
        return out

    out["action_kind"] = "evidence"
    out["allowed_tools"] = ["coding_finish", "coding_update_plan"]
    out["hypothesis_causal_targets"] = []
    out["hypothesis_causal_evidence_linked"] = False
    out.pop("hypothesis_causal_evidence_ranges", None)
    out["hypothesis_evidence_range_required"] = True
    out["hypothesis_evidence_range_targets"] = missing_range_targets
    out["required_action"] = (
        "The structured hypothesis cites a verified repository file but not the bounded line range "
        "that was actually read. Do not inspect further. Revise Repository evidence with "
        "coding_update_plan so it cites the full repository-relative path plus a line or line range "
        "contained in the verified read (for example path/to/file.py:120-145), and describe the "
        "specific finding supported by those lines."
    )
    return out


def install(evidence_policy: Any) -> None:
    """Install range-qualified provenance after the existing freshness overlay."""
    if bool(getattr(evidence_policy, "_coding_evidence_range_provenance_installed", False)):
        return

    original_apply = evidence_policy.apply_provenance_gate

    def apply_with_range_provenance(
        forced_action: Any,
        task: Mapping[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        effective = original_apply(forced_action, task, state)
        return refine_state(evidence_policy, forced_action, task, effective)

    evidence_policy.apply_provenance_gate = apply_with_range_provenance

    original_prompt = evidence_policy._provenance_prompt_context

    def prompt_with_range_provenance(base: Any, state: Mapping[str, Any]) -> str:
        if not state.get("hypothesis_evidence_range_required"):
            return original_prompt(base, state)
        ranges = state.get("causal_evidence_ranges") or []
        rendered = ", ".join(
            f"{item.get('path')}:{item.get('start_line')}-{item.get('end_line')}"
            for item in ranges
            if isinstance(item, Mapping) and item.get("path")
        )
        allowed = ", ".join(state.get("allowed_tools") or [])
        return (
            "Controller forced-action mode is ACTIVE. A file path alone is not sufficient "
            "provenance for a bounded source read. The current Repository evidence does not cite "
            "a verified line span. Do not inspect further. Revise the four-field structured "
            "hypothesis with coding_update_plan and cite at least one verified span in Repository "
            f"evidence using path:start-end syntax. Verified spans: {rendered or '(none)'}. "
            f"Available tools: {allowed or 'coding_finish'}."
        )

    evidence_policy._provenance_prompt_context = prompt_with_range_provenance
    evidence_policy._coding_evidence_range_provenance_installed = True
