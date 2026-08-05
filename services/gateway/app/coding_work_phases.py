from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Sequence


SCHEMA = "nexus_coding_work_phase.v1"
DISCOVERY = "discovery"
DECISION = "decision"
EXECUTION = "execution"
_PHASES = {DISCOVERY, DECISION, EXECUTION}
_EVIDENCE_TOOLS = {
    "coding_search_text",
    "coding_read_file",
    "coding_read_file_lines",
    "coding_list_tree",
    "coding_git_status",
    "coding_git_diff",
    "coding_change_summary",
    "coding_run_command",
}
_EDIT_TOOLS = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def mission_requires_file_changes(task: Mapping[str, Any]) -> bool:
    mission = task.get("mission") if isinstance(task.get("mission"), dict) else {}
    completion = mission.get("completion_policy") if isinstance(mission.get("completion_policy"), dict) else {}
    if "require_file_changes" in completion:
        return bool(completion.get("require_file_changes"))
    return True


def _stored_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    raw = task.get("agent_work_phase") if isinstance(task.get("agent_work_phase"), dict) else {}
    phase = str(raw.get("phase") or "").strip().lower()
    if str(raw.get("schema") or "") != SCHEMA or phase not in _PHASES:
        return {}
    return dict(raw)


def phase_state(
    task: Mapping[str, Any],
    *,
    phase: str = "",
    decision: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    stored = _stored_state(task)
    normalized = str(phase or stored.get("phase") or DISCOVERY).strip().lower()
    if normalized not in _PHASES:
        normalized = DISCOVERY
    return {
        "schema": SCHEMA,
        "phase": normalized,
        "decision": str(decision or stored.get("decision") or "").strip(),
        "reason": str(reason or stored.get("reason") or "").strip(),
    }


def current_phase(task: Mapping[str, Any]) -> str:
    return str(phase_state(task).get("phase") or DISCOVERY)


def _current_run_events(task: Mapping[str, Any]) -> list[Dict[str, Any]]:
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    run_id = str(task.get("agent_run_id") or "").strip()
    start = 0
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if str(event.get("type") or "") != "started":
            continue
        event_run_id = str(event.get("run_id") or "").strip()
        if not run_id or not event_run_id or event_run_id == run_id:
            start = index
            break
    return events[start:]


def _has_edit(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        str(event.get("type") or "") == "tool_started"
        and str(event.get("name") or "") in _EDIT_TOOLS
        for event in events
    )


def advance_phase(
    task: Mapping[str, Any],
    *,
    stage: str,
    events: Sequence[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    prior = phase_state(task)
    phase = str(prior.get("phase") or DISCOVERY)
    event_list = list(events) if events is not None else _current_run_events(task)
    if _has_edit(event_list) or phase == EXECUTION:
        return phase_state(task, phase=EXECUTION, decision="remediate", reason="workspace execution has begun")
    enforced = str(stage or "observe") in {"interrupt", "recovery", "continuation"}
    if phase == DECISION:
        if mission_requires_file_changes(task):
            return phase_state(task, phase=EXECUTION, decision="remediate", reason="mission requires repository changes")
        return phase_state(task, phase=DECISION, decision="report_only", reason="review mission may complete without changes")
    if enforced:
        if mission_requires_file_changes(task):
            return phase_state(task, phase=EXECUTION, decision="remediate", reason="discovery produced a bounded remediation decision")
        return phase_state(task, phase=DECISION, decision="report_only", reason="discovery produced a bounded review decision")
    return phase_state(task, phase=DISCOVERY, decision="", reason="collect bounded repository evidence")


def discovery_evidence_fingerprint(task: Mapping[str, Any]) -> str:
    if current_phase(task) != DISCOVERY:
        return ""
    signatures: set[str] = set()
    for item in task.get("agent_inspection_ledger") if isinstance(task.get("agent_inspection_ledger"), list) else []:
        if isinstance(item, dict) and str(item.get("signature") or "").strip():
            signatures.add("ledger:" + str(item.get("signature")))
    for event in _current_run_events(task):
        event_type = str(event.get("type") or "")
        name = str(event.get("name") or "")
        if event_type == "tool_started" and name in _EVIDENCE_TOOLS:
            signatures.add("start:" + _stable_hash({"name": name, "args": event.get("args") or {}}))
        if event_type == "tool_finished" and name == "coding_run_command":
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            evidence = {
                "name": name,
                "ok": result.get("ok"),
                "returncode": result.get("returncode"),
                "error": str(result.get("error") or "")[:500],
                "stdout": str(result.get("stdout") or "")[:2000],
                "stderr": str(result.get("stderr") or "")[:2000],
            }
            signatures.add("validation-result:" + _stable_hash(evidence))
    return _stable_hash(sorted(signatures)) if signatures else ""


def phase_prompt(task: Mapping[str, Any]) -> str:
    state = phase_state(task)
    phase = str(state.get("phase") or DISCOVERY)
    if phase == DISCOVERY:
        return (
            "Controller work phase: discovery. Gather bounded repository evidence, run targeted validation, "
            "and narrow findings; edits are optional until an actionable defect is supported."
        )
    if phase == DECISION:
        return (
            "Controller work phase: decision. Classify findings as confirmed actionable defects, one specified "
            "remaining check, environment/configuration blockers, or disproven; then report or remediate according to the mission."
        )
    return (
        "Controller work phase: execution. Make the smallest evidence-backed edit, run targeted validation, "
        "review the resulting diff, and finish; do not reopen broad repository exploration."
    )
