from __future__ import annotations

from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class ProgressObservation:
    cycle: int
    workspace_fingerprint: str
    plan_revision: int
    validation_revision: int
    diff_review_revision: int
    finish_state: str
    guidance_revision: float
    evidence_fingerprint: str = ""
    work_phase: str = "discovery"


@dataclass(frozen=True)
class ProgressState:
    observation: ProgressObservation | None
    stagnant_cycles: int = 0


@dataclass(frozen=True)
class ProgressDecision:
    state: ProgressState
    progressed: bool
    pause: bool
    reason_code: str = ""
    summary: str = ""


def evaluate_cycle_progress(
    previous: ProgressState,
    current: ProgressObservation,
    *,
    max_stagnant_cycles: int,
) -> ProgressDecision:
    prior = previous.observation
    guided = prior is not None and current.guidance_revision > prior.guidance_revision
    progressed = guided or prior is None or any(
        (
            current.workspace_fingerprint != prior.workspace_fingerprint,
            current.plan_revision > prior.plan_revision,
            current.validation_revision > prior.validation_revision,
            current.diff_review_revision > prior.diff_review_revision,
            current.finish_state != prior.finish_state,
            current.work_phase != prior.work_phase,
            (
                current.work_phase == "discovery"
                and current.evidence_fingerprint != prior.evidence_fingerprint
            ),
        )
    )
    # Cycle numbering restarts for every new agent run. The durable observation
    # remains useful for detecting actual progress, but the exhausted
    # no-progress allowance belongs to the run that accumulated it.
    new_run = prior is not None and current.cycle <= prior.cycle
    prior_stagnant_cycles = 0 if new_run else previous.stagnant_cycles
    stagnant = 0 if progressed else prior_stagnant_cycles + 1
    pause = stagnant >= max(1, int(max_stagnant_cycles))
    return ProgressDecision(
        state=ProgressState(current, stagnant),
        progressed=progressed,
        pause=pause,
        reason_code="no_progress_limit" if pause else "",
        summary=(
            f"Coding run paused after {stagnant} cycles without a durable state transition."
            if pause
            else ""
        ),
    )


def progress_state_from_dict(value: Any) -> ProgressState:
    raw = value if isinstance(value, dict) else {}
    observation_raw = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    observation = None
    if observation_raw is not None:
        observation = ProgressObservation(
            cycle=_as_int(observation_raw.get("cycle")),
            workspace_fingerprint=str(observation_raw.get("workspace_fingerprint") or ""),
            plan_revision=_as_int(observation_raw.get("plan_revision")),
            validation_revision=_as_int(observation_raw.get("validation_revision")),
            diff_review_revision=_as_int(observation_raw.get("diff_review_revision")),
            finish_state=str(observation_raw.get("finish_state") or "running"),
            guidance_revision=_as_float(observation_raw.get("guidance_revision")),
            evidence_fingerprint=str(observation_raw.get("evidence_fingerprint") or ""),
            work_phase=str(observation_raw.get("work_phase") or "discovery"),
        )
    return ProgressState(
        observation=observation,
        stagnant_cycles=max(0, _as_int(raw.get("stagnant_cycles"))),
    )


def progress_state_to_dict(state: ProgressState) -> Dict[str, Any]:
    return {
        "observation": asdict(state.observation) if state.observation is not None else None,
        "stagnant_cycles": state.stagnant_cycles,
    }


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


def _legacy_reason_code(status: str, summary: str, error: str, terminal: Dict[str, Any]) -> str:
    combined = " ".join((status, summary, error, str(terminal.get("finalization_status") or ""))).lower()
    if (
        "no substantive progress" in combined
        or "without substantive progress" in combined
        or "without a durable state transition" in combined
        or "no_progress_limit" in combined
    ):
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
        "no_progress_limit": "Review the durable project plan and recent cycle history, provide a concrete next edit or blocker decision, and resume only after breaking the repeated inspection loop.",
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
    structured_reason = str(
        run.get("stop_reason_code")
        or event.get("stop_reason_code")
        or event.get("reason_code")
        or task.get("agent_stop_reason_code")
        or terminal.get("stop_reason_code")
        or ""
    ).strip()

    if no_progress and not structured_reason:
        no_progress_ts = _as_float(no_progress.get("ts"))
        terminal_ts = max(_as_float(run.get("finished_at")), _as_float(event.get("ts")))
        if not terminal_ts or no_progress_ts >= terminal_ts - 5:
            summary = str(no_progress.get("summary") or summary).strip()
            error = str(no_progress.get("error") or error).strip()

    summary = redact(summary)[:2000]
    error = redact(error)[:2000]
    reason_code = structured_reason or _legacy_reason_code(status, summary, error, terminal)
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


def redacted_archive_run(
    task: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    redact: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    redact = redact or (lambda value: value)
    run = _terminal_run(task, archived_at=_as_float(manifest.get("archived_at")))
    if not run:
        return {}
    return {
        "run_id": str(run.get("run_id") or ""),
        "status": str(run.get("status") or ""),
        "stop_reason_code": str(run.get("stop_reason_code") or ""),
        "cycle": _as_int(run.get("cycle")),
        "finished_at": _as_float(run.get("finished_at")),
        "summary": redact(str(run.get("summary") or ""))[:2000],
        "error": redact(str(run.get("error") or ""))[:2000],
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
    severity = (
        "info"
        if diagnostics.get("reason_code") == "run_completed"
        else "error"
        if diagnostics.get("error")
        else "warn"
    )
    return {
        "severity": severity,
        "code": f"workspace_stop_{diagnostics.get('reason_code') or 'unknown_stop'}",
        "summary": f"Workspace stopped because: {diagnostics.get('summary') or diagnostics.get('reason_code') or 'unknown reason'}",
        "evidence": evidence[:8],
    }
