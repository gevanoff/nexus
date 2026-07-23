from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


_ARCHIVE_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "failed_finalization",
    "failed_publish",
    "idle_waiting",
    "interrupted",
    "paused",
    "stopped",
}

_READ_ONLY_TOOLS = {
    "coding_list_tree",
    "coding_tool_manifest",
    "coding_read_file",
    "coding_read_file_lines",
    "coding_search_text",
    "coding_git_status",
    "coding_state_snapshot",
    "coding_web_browse",
}

_INSTALLED = False


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _terminal_run(task: Dict[str, Any], *, archived_at: float) -> Dict[str, Any]:
    runs = task.get("agent_runs") if isinstance(task.get("agent_runs"), list) else []
    candidates: List[Tuple[float, int, Dict[str, Any]]] = []
    for index, raw in enumerate(runs):
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").strip().lower()
        if status not in _ARCHIVE_TERMINAL_STATUSES:
            continue
        finished_at = _as_float(raw.get("finished_at") or raw.get("created_at") or raw.get("started_at"))
        if archived_at > 0 and finished_at > archived_at + 1:
            continue
        candidates.append((finished_at, index, raw))
    return dict(max(candidates, key=lambda item: (item[0], item[1]))[2]) if candidates else {}


def _terminal_event(task: Dict[str, Any], *, archived_at: float) -> Dict[str, Any]:
    events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
    event_types = {
        "budget_exhausted",
        "completed",
        "failed",
        "interrupted",
        "no_progress_limit",
        "paused",
        "stopped",
    }
    candidates: List[Tuple[float, int, Dict[str, Any]]] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            continue
        event_type = str(raw.get("type") or "").strip().lower()
        if event_type not in event_types:
            continue
        ts = _as_float(raw.get("ts"))
        if archived_at > 0 and ts > archived_at + 1:
            continue
        candidates.append((ts, index, raw))
    return dict(max(candidates, key=lambda item: (item[0], item[1]))[2]) if candidates else {}


def _latest_no_progress_event(task: Dict[str, Any], *, archived_at: float) -> Dict[str, Any]:
    events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
    matches: List[Tuple[float, int, Dict[str, Any]]] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict) or str(raw.get("type") or "") != "no_progress_limit":
            continue
        ts = _as_float(raw.get("ts"))
        if archived_at > 0 and ts > archived_at + 1:
            continue
        matches.append((ts, index, raw))
    return dict(max(matches, key=lambda item: (item[0], item[1]))[2]) if matches else {}


def _reason_code(status: str, summary: str, error: str, terminal: Dict[str, Any]) -> str:
    combined = " ".join((status, summary, error, str(terminal.get("finalization_status") or ""))).lower()
    if "no substantive progress" in combined or "without substantive progress" in combined or "no_progress_limit" in combined:
        return "no_progress_limit"
    if "wall-clock budget" in combined or "wall_clock_budget" in combined:
        return "wall_clock_budget"
    if "cycle budget" in combined or "cycle_budget" in combined:
        return "cycle_budget"
    if "mlx_glm_input_too_large" in combined or "input exceeds the interactive latency guard" in combined:
        return "input_too_large"
    if "server disconnected" in combined or "disconnected without sending a response" in combined:
        return "upstream_disconnected"
    if "gateway restarted" in combined:
        return "gateway_restart"
    if "gateway stopped" in combined:
        return "gateway_stopped"
    if "internal_error" in combined or "'status': 500" in combined or '"status":500' in combined:
        return "upstream_internal_error"
    finalization_status = str(terminal.get("finalization_status") or "").strip().lower()
    if finalization_status in {"failed_finalization", "failed_publish"}:
        return finalization_status
    if status == "completed":
        return "run_completed"
    if status == "failed":
        return "agent_failed"
    if status == "interrupted":
        return "run_interrupted"
    if status == "idle_waiting":
        return "idle_waiting"
    if status in {"paused", "stopped"}:
        return "manual_or_unspecified_pause"
    return "unknown_stop"


def _remediation(reason_code: str, *, error: str) -> str:
    values = {
        "run_completed": "No remediation is required; verify the final diff, validation, commit, and publication records.",
        "no_progress_limit": "Review the durable project plan and recent tool history, provide a concrete next edit or blocker decision, and resume only after breaking the repeated inspection loop.",
        "wall_clock_budget": "Resume only after reviewing project-plan and no-progress evidence; narrow the next action or switch models if no edits were produced.",
        "cycle_budget": "Resume with a narrowed next action, or increase the cycle budget only after confirming substantive progress.",
        "input_too_large": "Compact the agent context, reduce repeated file/tool evidence, or select a route with a larger accepted input budget.",
        "upstream_disconnected": "Check backend process and network health, then retry from the durable checkpoint.",
        "upstream_internal_error": "Inspect backend logs and health, restart or repair the backend if needed, then resume from the durable checkpoint.",
        "gateway_restart": "Allow gateway recovery to resume from durable state, or start a continuation run if recovery did not occur.",
        "gateway_stopped": "Restore the gateway service and resume from durable workspace state.",
        "failed_finalization": "Correct the finalization error, then rerun validation, diff review, commit, and publication.",
        "failed_publish": "Correct repository authentication, permissions, remote, or pull-request settings, then retry publication.",
        "agent_failed": "Address the recorded agent error and resume from the durable checkpoint with targeted guidance.",
        "run_interrupted": "Resolve the interruption source and resume from durable workspace state.",
        "idle_waiting": "Supply the missing user decision or guidance recorded by the run, then resume.",
        "manual_or_unspecified_pause": "Confirm whether the pause was intentional; resume from durable state when ready.",
        "unknown_stop": "Inspect the retained terminal run and lifecycle events before resuming or remediating.",
    }
    result = values.get(reason_code, values["unknown_stop"])
    if error and reason_code in {"agent_failed", "failed_finalization", "failed_publish", "unknown_stop"}:
        return f"{result} Recorded error: {error[:500]}"
    return result


def archive_stop_diagnostics(
    task: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    redact: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    redact = redact or (lambda value: value)
    archived_at = _as_float(manifest.get("archived_at"))
    run = _terminal_run(task, archived_at=archived_at)
    event = _terminal_event(task, archived_at=archived_at)
    no_progress = _latest_no_progress_event(task, archived_at=archived_at)
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}

    status = str(
        run.get("status")
        or event.get("type")
        or task.get("agent_status")
        or manifest.get("agent_status")
        or task.get("status")
        or manifest.get("status")
        or "unknown"
    ).strip().lower()
    if status in {"budget_exhausted", "no_progress_limit"}:
        status = "paused"

    summary = str(run.get("summary") or event.get("summary") or task.get("agent_summary") or "").strip()
    error = str(
        run.get("error")
        or event.get("error")
        or task.get("agent_error")
        or terminal.get("finalization_error")
        or ""
    ).strip()

    if no_progress:
        no_progress_ts = _as_float(no_progress.get("ts"))
        terminal_ts = max(_as_float(run.get("finished_at")), _as_float(event.get("ts")))
        if not terminal_ts or no_progress_ts >= terminal_ts - 5:
            summary = str(no_progress.get("summary") or summary).strip()
            error = str(no_progress.get("error") or error).strip()

    summary = redact(summary)[:2000]
    error = redact(error)[:2000]
    reason_code = _reason_code(status, summary, error, terminal)
    if not summary:
        summary = {
            "completed": "Coding run completed.",
            "failed": "Coding run failed.",
            "interrupted": "Coding run was interrupted.",
            "paused": "Coding run was paused.",
            "stopped": "Coding run was stopped.",
        }.get(status, f"Archived workspace recorded terminal status {status or 'unknown'}.")

    source = "agent_run" if run else "agent_event" if event else "task_status"
    cycle_value = run.get("cycle") if run else event.get("cycle")
    return {
        "status": status or "unknown",
        "reason_code": reason_code,
        "summary": summary,
        "error": error,
        "remediation": _remediation(reason_code, error=error),
        "run_id": str(run.get("run_id") or event.get("run_id") or ""),
        "cycle": _as_int(cycle_value),
        "finished_at": _as_float(run.get("finished_at") or event.get("ts")),
        "source": source,
    }


def archive_stop_finding(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    evidence = [
        f"status={diagnostics.get('status') or 'unknown'}",
        f"reason={diagnostics.get('reason_code') or 'unknown_stop'}",
    ]
    if diagnostics.get("run_id"):
        evidence.append(f"run_id={diagnostics['run_id']}")
    if diagnostics.get("cycle"):
        evidence.append(f"cycle={diagnostics['cycle']}")
    if diagnostics.get("error"):
        evidence.append(f"error={diagnostics['error']}")
    evidence.append(f"remediation={diagnostics.get('remediation') or ''}")
    severity = "info" if diagnostics.get("reason_code") == "run_completed" else "error" if diagnostics.get("error") else "warn"
    return {
        "severity": severity,
        "code": f"workspace_stop_{diagnostics.get('reason_code') or 'unknown_stop'}",
        "summary": f"Workspace stopped because: {diagnostics.get('summary') or diagnostics.get('reason_code') or 'unknown reason'}",
        "evidence": evidence[:8],
    }


def _configured_no_progress_limit(task: Dict[str, Any], cw: Any) -> int:
    mission = cw.normalize_coding_mission(task)
    policy = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
    return max(1, _as_int(policy.get("max_no_progress_cycles") or 8))


def _changed_count(result: Dict[str, Any]) -> int:
    changes = result.get("changes") if isinstance(result.get("changes"), dict) else {}
    counts = changes.get("counts") if isinstance(changes.get("counts"), dict) else {}
    return _as_int(counts.get("total"))


def _substantive_tool_result(name: str, args: Dict[str, Any], result: Dict[str, Any], *, before: Dict[str, Any], after: Dict[str, Any], ca: Any) -> bool:
    if not bool(result.get("ok")) and name != "coding_run_command":
        return False
    if name == "coding_write_file":
        return bool(result.get("ok"))
    if name == "coding_replace_text":
        return _as_int(result.get("replacements")) > 0
    if name == "coding_apply_patch":
        if bool(args.get("check_only") or result.get("check_only")):
            return False
        applied = result.get("apply") if isinstance(result.get("apply"), dict) else result
        return bool(applied.get("ok", result.get("ok")))
    if name == "coding_update_plan":
        before_plan = before.get("project_plan") if isinstance(before.get("project_plan"), dict) else {}
        after_plan = after.get("project_plan") if isinstance(after.get("project_plan"), dict) else {}
        return _as_int(after_plan.get("revision")) > _as_int(before_plan.get("revision"))
    if name == "coding_run_command":
        if not bool(ca._is_validation_command(args.get("argv"))):
            return False
        signature = json.dumps(args.get("argv") or [], ensure_ascii=False, separators=(",", ":"))
        return signature != str(before.get("agent_no_progress_last_validation_signature") or "")
    if name == "coding_git_diff":
        return False
    if name in {"coding_commit", "coding_push", "coding_create_pull_request", "coding_finish"}:
        return bool(result.get("ok"))
    return False


def _record_tool_outcome(task_id: str, name: str, args: Dict[str, Any], result: Dict[str, Any], *, before: Dict[str, Any], cw: Any, ca: Any) -> None:
    after = cw.load_task(task_id)
    now = time.time()
    previous_updated_at = _as_float(before.get("agent_no_progress_updated_at"))
    guidance_at = _as_float(after.get("last_guidance_at"))
    current = _as_int(before.get("agent_no_progress_cycles"))
    if guidance_at > previous_updated_at:
        current = 0

    substantive = _substantive_tool_result(name, args, result, before=before, after=after, ca=ca)
    if substantive:
        next_count = 0
        pause_reason = ""
    else:
        next_count = current + 1
        limit = _configured_no_progress_limit(after, cw)
        pause_reason = (
            f"Coding run paused after {next_count} tool actions without substantive progress. "
            "The agent inspected repository state but did not edit files, advance the project plan, validate changes, review a meaningful diff, or finish. "
            "Provide a concrete next edit or blocker decision before resuming."
            if next_count >= limit
            else ""
        )

    def apply(task: Dict[str, Any]) -> None:
        task["agent_no_progress_cycles"] = next_count
        task["agent_no_progress_updated_at"] = now
        task["agent_no_progress_last_tool"] = name
        if substantive:
            task["agent_pause_reason"] = ""
            if name in {"coding_write_file", "coding_replace_text", "coding_apply_patch"}:
                task["agent_no_progress_last_validation_signature"] = ""
            elif name == "coding_run_command":
                task["agent_no_progress_last_validation_signature"] = json.dumps(args.get("argv") or [], ensure_ascii=False, separators=(",", ":"))
        elif pause_reason:
            task["agent_pause_reason"] = pause_reason
            task["agent_pause_requested"] = True

    cw.mutate_task(task_id, apply)
    if substantive and current:
        ca._append_event(task_id, {"type": "progress_made", "summary": f"Substantive progress recorded by {name}.", "name": name, "previous_no_progress_cycles": current})
    elif pause_reason:
        ca._append_event(task_id, {"type": "no_progress_limit", "summary": pause_reason, "name": name, "count": next_count, "limit": _configured_no_progress_limit(after, cw)})
    else:
        ca._append_event(task_id, {"type": "no_progress_cycle", "summary": f"No substantive progress recorded for {name}.", "name": name, "count": next_count})


def install_coding_runtime_guardrails() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app import coding_agent as ca
    from app import coding_workspace as cw

    original_heuristics = cw._archive_heuristic_findings
    original_inspect = cw.inspect_archived_task
    original_run_tool = ca._run_tool
    original_raise_if_paused = ca._raise_if_paused

    def guarded_heuristics(task: Dict[str, Any], manifest: Dict[str, Any], diff_snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        diagnostics = archive_stop_diagnostics(task, manifest, redact=cw._redact_text)
        return [archive_stop_finding(diagnostics), *original_heuristics(task, manifest, diff_snapshot)]

    def guarded_inspect(archive_id: str, *, max_diff_chars: int = 12000) -> Dict[str, Any]:
        snapshot = original_inspect(archive_id, max_diff_chars=max_diff_chars)
        archive = snapshot.get("archive") if isinstance(snapshot.get("archive"), dict) else {}
        task_path = str(((archive.get("paths") or {}).get("task") if isinstance(archive.get("paths"), dict) else "") or "")
        manifest_path = str(((archive.get("paths") or {}).get("manifest") if isinstance(archive.get("paths"), dict) else "") or "")
        task = cw._load_archived_task_json(Path(task_path)) if task_path else {}
        raw_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8")) if manifest_path and Path(manifest_path).exists() else {}
        manifest = cw._normalize_archive_manifest(raw_manifest if isinstance(raw_manifest, dict) else {}, manifest_path=Path(manifest_path), task=task)
        diagnostics = archive_stop_diagnostics(task, manifest, redact=cw._redact_text)
        snapshot["stop_diagnostics"] = diagnostics
        task_summary = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
        task_summary.update(
            {
                "agent_summary": str(task.get("agent_summary") or ""),
                "agent_error": cw._redact_text(str(task.get("agent_error") or "")),
                "terminal_result": task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {},
                "agent_runs": [item for item in (task.get("agent_runs") or [])[-30:] if isinstance(item, dict)],
            }
        )
        snapshot["task"] = task_summary
        return snapshot

    def guarded_run_tool(task_id: str, name: str, args: Dict[str, Any], *, git_token_value: Optional[str]) -> Dict[str, Any]:
        before = cw.load_task(task_id)
        result = original_run_tool(task_id, name, args, git_token_value=git_token_value)
        _record_tool_outcome(task_id, name, args, result, before=before, cw=cw, ca=ca)
        return result

    def guarded_raise_if_paused(task_id: str) -> None:
        task = cw.load_task(task_id)
        if bool(task.get("agent_pause_requested") or task.get("agent_stop_requested")) and str(task.get("agent_pause_reason") or "").strip():
            raise ca._CodingAgentPaused(str(task.get("agent_pause_reason") or "").strip())
        original_raise_if_paused(task_id)

    cw._archive_heuristic_findings = guarded_heuristics
    cw.inspect_archived_task = guarded_inspect
    ca._run_tool = guarded_run_tool
    ca._raise_if_paused = guarded_raise_if_paused
    _INSTALLED = True
