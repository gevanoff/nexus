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

    ranges: dict[str, list[tuple[int, int]]] = {}
    legacy_paths: set[str] = set()
    for event in events[window_start:]:
        result = _successful_read(event)
        if not result:
            continue
        path = _normalized_path(result.get("path"))
        if not path:
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


def _repository_evidence(forced_action: Any, task: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    base = getattr(forced_action, "_execution_provenance_base", forced_action)
    parser = getattr(base, "_structured_hypothesis", None)
    if not callable(parser):
        return ""
    try:
        ready, fields = parser(task, state)
    except Exception:
        return ""
    if not ready or not isinstance(fields, Mapping):
        return ""
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


def refine_state(
    evidence_policy: Any,
    forced_action: Any,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Require modern bounded reads to be cited at their verified line range.

    The original provenance gate proves that a file was read, but a path-only
    claim can otherwise treat a tiny bounded read as verification of unrelated
    code elsewhere in a large file. Modern ``coding_read_file_lines`` results
    already persist start/end lines, so bind the structured hypothesis to those
    spans. Legacy persisted reads without range metadata retain path-only
    behavior for compatibility.
    """
    out = dict(state)
    ranges, legacy_paths = _verified_ranges(evidence_policy, forced_action, task, out)
    if ranges:
        out["causal_evidence_ranges"] = _range_metadata(ranges)

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
    matched_targets: list[str] = []
    matched_ranges: list[dict[str, Any]] = []
    missing_range_targets: list[str] = []
    for target in linked_targets:
        verified = ranges.get(target) or []
        if not verified:
            # Old durable events and synthetic test fixtures may not carry line
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

    if matched_targets:
        out["hypothesis_causal_targets"] = matched_targets
        out["hypothesis_causal_evidence_linked"] = True
        if matched_ranges:
            out["hypothesis_causal_evidence_ranges"] = matched_ranges
        return out

    if not missing_range_targets:
        return out

    out["action_kind"] = "evidence"
    out["allowed_tools"] = ["coding_finish", "coding_update_plan"]
    out["hypothesis_causal_targets"] = []
    out["hypothesis_causal_evidence_linked"] = False
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
