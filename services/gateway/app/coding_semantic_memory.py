from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from app import coding_stagnation_resilience as resilience
from app import coding_workspace as cw
from app.config import S, logger


_RUNTIME_TASK: Optional[asyncio.Task[Any]] = None
_ACTIVE_STATUSES = {"queued", "running", "stopping", "pausing"}


def _clip(value: Any, limit: int) -> str:
    return resilience.clip(value, limit)


def _as_int(value: Any) -> int:
    return resilience.as_int(value)


def _poll_interval() -> float:
    try:
        return max(0.5, min(float(getattr(S, "CODING_SEMANTIC_MEMORY_POLL_SEC", 2.0) or 2.0), 30.0))
    except Exception:
        return 2.0


def _max_no_progress_cycles(task: Dict[str, Any]) -> int:
    mission = cw.normalize_coding_mission(task)
    budget = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
    return max(2, _as_int(budget.get("max_no_progress_cycles") or 8))


def _stagnation_threshold(task: Dict[str, Any]) -> int:
    maximum = _max_no_progress_cycles(task)
    configured = _as_int(getattr(S, "CODING_SEMANTIC_MEMORY_STAGNANT_CYCLES", 0) or 0)
    if configured > 0:
        return max(1, min(configured, maximum - 1))
    return resilience.stage_thresholds(maximum).assist


def _current_run_events(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    return resilience.current_run_events(task)


def _sample_metrics(controller: Dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        _as_int(controller.get("last_cycle")),
        max(
            _as_int(controller.get("processed_event_total")),
            _as_int(controller.get("processed_event_count")),
        ),
        _as_int(controller.get("plan_revision")),
        _as_int(controller.get("progress_stagnant_cycles")),
    )


def _same_controller_stream(existing: Dict[str, Any], prepared: Dict[str, Any]) -> bool:
    return (
        str(existing.get("run_id") or "") == str(prepared.get("run_id") or "")
        and str(existing.get("state_key") or "") == str(prepared.get("state_key") or "")
    )


def _sample_is_stale(existing: Dict[str, Any], prepared: Dict[str, Any]) -> bool:
    return _same_controller_stream(existing, prepared) and _sample_metrics(prepared) < _sample_metrics(existing)


def _sample_is_newer(existing: Dict[str, Any], prepared: Dict[str, Any]) -> bool:
    return not _same_controller_stream(existing, prepared) or _sample_metrics(prepared) > _sample_metrics(existing)


def _observation_complete(task: Dict[str, Any], prepared: Dict[str, Any]) -> bool:
    existing = task.get("agent_stagnation_controller") if isinstance(task.get("agent_stagnation_controller"), dict) else {}
    cursor = str(prepared.get("processed_event_cursor") or "")
    return (
        (not cursor or str(existing.get("processed_event_cursor") or "") == cursor)
        and isinstance(task.get("agent_inspection_ledger"), list)
        and isinstance(task.get("agent_working_memory"), dict)
        and isinstance(task.get("agent_context_manifest"), dict)
    )


def _prepare_checkpoint(task: Dict[str, Any]) -> Dict[str, Any]:
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    run_id = str(task.get("agent_run_id") or "")
    cycle = _as_int(task.get("agent_cycle"))
    events = _current_run_events(task)
    raw_controller = (
        task.get("agent_stagnation_controller")
        if isinstance(task.get("agent_stagnation_controller"), dict)
        else {}
    )
    previous_run_id = str(raw_controller.get("run_id") or "")
    new_events = resilience.new_events_since(events, raw_controller, run_id=run_id)
    previous_event_total = (
        max(
            _as_int(raw_controller.get("processed_event_total")),
            _as_int(raw_controller.get("processed_event_count")),
        )
        if previous_run_id == run_id
        else 0
    )
    ledger = resilience.update_inspection_ledger(
        task.get("agent_inspection_ledger"),
        new_events,
        run_id=run_id,
        cycle=cycle,
    )
    state_key = resilience.durable_state_key(task)
    classification = resilience.classify_stagnation(task, events, ledger)
    controller = resilience.advance_controller(
        task,
        state_key=state_key,
        run_id=run_id,
        cycle=cycle,
        progress_stagnant_cycles=_as_int(progress.get("stagnant_cycles")),
        classification=classification,
        max_no_progress_cycles=_max_no_progress_cycles(task),
    )
    controller["processed_event_count"] = len(events)
    controller["processed_event_total"] = previous_event_total + len(new_events)
    controller["processed_event_cursor"] = (
        resilience.event_fingerprint(events[-1]) if events else ""
    )

    current_plan_revision = _as_int(resilience.progress_observation(task).get("plan_revision"))
    previous_plan_revision = _as_int(raw_controller.get("plan_revision"))
    kind = resilience.intervention_kind(task, controller)
    same_state_new_run = (
        bool(previous_run_id)
        and previous_run_id != run_id
        and str(raw_controller.get("state_key") or "") == state_key
    )
    suppressed_run = str(raw_controller.get("suppress_interventions_for_run") or "")
    if same_state_new_run and kind != "continuation":
        controller["suppress_interventions_for_run"] = run_id
        kind = "observe"
    elif (
        suppressed_run
        and suppressed_run == run_id
        and str(raw_controller.get("state_key") or "") == state_key
    ):
        controller["suppress_interventions_for_run"] = run_id
        kind = "observe"
    elif (
        bool(raw_controller)
        and kind not in {"observe", "continuation", "recovery"}
        and current_plan_revision > previous_plan_revision
    ):
        plan_intervention = resilience.intervention_id(
            state_key,
            "plan_checkpoint",
            run_id=run_id,
        )
        # A note-only plan revision can receive one checkpoint for orientation,
        # but later revisions in the same durable state must not fall through
        # into assist/interrupt credit.
        kind = (
            "observe"
            if resilience.intervention_already_claimed(raw_controller, plan_intervention)
            else "plan_checkpoint"
        )
    controller["intervention_kind"] = kind

    working_memory = resilience.build_working_memory(
        task,
        state_key=state_key,
        controller=controller,
        ledger=ledger,
        events=events,
    )
    manifest = resilience.build_context_manifest(
        task,
        state_key=state_key,
        working_memory=working_memory,
        events=events,
    )
    # Guidance credit is per run. A resumed run must not inherit prior-run
    # assist/interrupt/recovery claims for the same durable output state.
    # Recovery leases remain state-keyed in _claim_checkpoint so unchanged
    # state cannot gain an unlimited number of guardrail transitions.
    intervention = resilience.intervention_id(state_key, kind, run_id=run_id)
    inspected = [
        _clip(item.get("target"), 240)
        for item in ledger[-12:]
        if isinstance(item, dict) and str(item.get("target") or "").strip()
    ]
    notes = [
        _clip(value, 500)
        for value in (working_memory.get("findings") or [])
        if str(value or "").strip()
    ]
    return {
        "schema": "nexus_coding_investigation_checkpoint.v2",
        "run_id": run_id,
        "generated_at": time.time(),
        "cycle": cycle,
        "stagnant_cycles": _as_int(controller.get("cycles")),
        "state_key": state_key,
        "classification": classification,
        "stage": str(controller.get("stage") or "observe"),
        "intervention_kind": kind,
        "intervention_id": intervention,
        "inspected_targets": inspected,
        "unverified_model_notes": notes,
        "active_plan_item": _clip(resilience.active_plan_summary(task), 500),
        "unresolved_question": str(working_memory.get("unresolved_question") or ""),
        "next_action": str(working_memory.get("next_action") or ""),
        "blocker": str(working_memory.get("blocker") or ""),
        "inspection_ledger": ledger,
        "controller": controller,
        "working_memory": working_memory,
        "context_manifest": manifest,
        "observation_changed": (
            _sample_is_newer(raw_controller, controller)
            or (bool(events) and not str(raw_controller.get("processed_event_cursor") or ""))
            or not _observation_complete(task, controller)
        ),
    }


def build_investigation_checkpoint(task: Dict[str, Any]) -> Dict[str, Any]:
    return _prepare_checkpoint(task)


def render_checkpoint_guidance(checkpoint: Dict[str, Any]) -> str:
    controller = checkpoint.get("controller") if isinstance(checkpoint.get("controller"), dict) else {}
    working_memory = (
        checkpoint.get("working_memory")
        if isinstance(checkpoint.get("working_memory"), dict)
        else {
            "findings": checkpoint.get("unverified_model_notes") or [],
            "inspected_targets": checkpoint.get("inspected_targets") or [],
            "unresolved_question": checkpoint.get("unresolved_question") or "",
            "next_action": checkpoint.get("next_action") or "",
            "blocker": checkpoint.get("blocker") or "",
            "revision": 1,
        }
    )
    manifest = (
        checkpoint.get("context_manifest")
        if isinstance(checkpoint.get("context_manifest"), dict)
        else {"preserved_event_count": 0, "omitted_event_count": 0}
    )
    return resilience.render_guidance(
        controller,
        working_memory,
        manifest,
        kind=str(checkpoint.get("intervention_kind") or checkpoint.get("stage") or "assist"),
    )


def _merge_controller_history(latest: Dict[str, Any], prepared: Dict[str, Any]) -> Dict[str, Any]:
    existing = (
        latest.get("agent_stagnation_controller")
        if isinstance(latest.get("agent_stagnation_controller"), dict)
        else {}
    )
    merged = dict(existing if _sample_is_stale(existing, prepared) else prepared)
    existing_history = [item for item in (existing.get("interventions") or []) if isinstance(item, dict)]
    prepared_history = [item for item in (prepared.get("interventions") or []) if isinstance(item, dict)]
    seen = set()
    history = []
    for item in [*existing_history, *prepared_history]:
        key = str(item.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        history.append(item)
    merged["interventions"] = history[-24:]
    return merged


def _persist_observation(task_id: str, checkpoint: Dict[str, Any]) -> bool:
    if not bool(checkpoint.get("observation_changed")):
        return False
    persisted = {"value": False}
    run_id = str(checkpoint.get("run_id") or "")
    state_key = str(checkpoint.get("state_key") or "")

    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
            return
        if str(task.get("agent_run_id") or "") != run_id:
            return
        if resilience.durable_state_key(task) != state_key:
            return
        controller = checkpoint.get("controller") if isinstance(checkpoint.get("controller"), dict) else {}
        existing = task.get("agent_stagnation_controller") if isinstance(task.get("agent_stagnation_controller"), dict) else {}
        if _sample_is_stale(existing, controller):
            return
        if not _sample_is_newer(existing, controller) and _observation_complete(task, controller):
            return
        task["agent_stagnation_controller"] = _merge_controller_history(task, controller)
        task["agent_inspection_ledger"] = list(checkpoint.get("inspection_ledger") or [])[-32:]
        task["agent_working_memory"] = dict(checkpoint.get("working_memory") or {})
        task["agent_context_manifest"] = dict(checkpoint.get("context_manifest") or {})
        persisted["value"] = True

    cw.mutate_task(task_id, apply)
    return persisted["value"]


def _claim_checkpoint(task_id: str, checkpoint: Dict[str, Any]) -> bool:
    claimed = {"value": False}
    state_key = str(checkpoint.get("state_key") or "")
    run_id = str(checkpoint.get("run_id") or "")
    intervention = str(checkpoint.get("intervention_id") or "")
    kind = str(checkpoint.get("intervention_kind") or checkpoint.get("stage") or "assist")
    guidance = render_checkpoint_guidance(checkpoint)

    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
            return
        if str(task.get("agent_run_id") or "") != run_id:
            return
        if resilience.durable_state_key(task) != state_key:
            return
        latest_controller = (
            task.get("agent_stagnation_controller")
            if isinstance(task.get("agent_stagnation_controller"), dict)
            else {}
        )
        if resilience.intervention_already_claimed(latest_controller, intervention):
            return

        controller = checkpoint.get("controller") if isinstance(checkpoint.get("controller"), dict) else {}
        if _sample_is_stale(latest_controller, controller):
            return
        controller = _merge_controller_history(task, controller)
        controller = resilience.append_intervention(
            controller,
            intervention=intervention,
            kind=kind,
            run_id=run_id,
            cycle=_as_int(checkpoint.get("cycle")),
        )
        now = time.time()
        task["agent_stagnation_controller"] = controller
        task["agent_inspection_ledger"] = list(checkpoint.get("inspection_ledger") or [])[-32:]
        task["agent_working_memory"] = dict(checkpoint.get("working_memory") or {})
        task["agent_context_manifest"] = dict(checkpoint.get("context_manifest") or {})
        task["agent_investigation_checkpoint"] = dict(checkpoint)
        task["agent_investigation_guidance_state_key"] = intervention

        events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
        events.append(
            {
                "type": (
                    "investigation_checkpoint"
                    if kind in {"assist", "plan_checkpoint", "continuation"}
                    else "stagnation_intervention"
                ),
                "ts": int(now),
                "cycle": checkpoint.get("cycle"),
                "stagnant_cycles": checkpoint.get("stagnant_cycles"),
                "state_key": state_key,
                "classification": checkpoint.get("classification"),
                "stage": checkpoint.get("stage"),
                "intervention_kind": kind,
                "intervention_id": intervention,
                "summary": "Persisted a staged stagnation checkpoint with durable working memory and inspection provenance.",
            }
        )
        task["agent_events"] = events[-1000:]

        messages = task.get("guidance_messages") if isinstance(task.get("guidance_messages"), list) else []
        messages.append(
            {
                "ts": now,
                "role": "user",
                "actor": "nexus-controller",
                "run_id": run_id,
                "content": guidance,
                "controller_intervention_id": intervention,
            }
        )
        task["guidance_messages"] = messages[-200:]
        # Controller guidance must be delivered to the model, but it is not
        # repository progress. Keep it separate from last_guidance_at, which is
        # part of the progress observation used by the agent loop.
        task["last_controller_guidance_at"] = now
        recovery_kind = "continuation" if kind == "continuation" else "checkpoint"
        recovery_id = resilience.intervention_id(state_key, f"recovery-{recovery_kind}")
        recovery_history = [str(value) for value in (task.get("agent_stagnation_recovery_history") or [])]
        if kind in {"assist", "continuation"} and recovery_id not in recovery_history:
            task["agent_stagnation_recovery_lease"] = {
                "schema": "nexus_coding_recovery_lease.v1",
                "id": recovery_id,
                "state_key": state_key,
                "kind": recovery_kind,
                "run_id": run_id,
                "granted_cycle": _as_int(checkpoint.get("cycle")),
                "remaining_transitions": 1,
                "status": "granted",
                "granted_at": now,
            }
            recovery_history.append(recovery_id)
            task["agent_stagnation_recovery_history"] = recovery_history[-16:]
        task.pop("agent_investigation_checkpoint_error", None)
        claimed["value"] = True

    cw.mutate_task(task_id, apply)
    return claimed["value"]


def _consume_recovery_lease(task_id: str, task: Dict[str, Any]) -> bool:
    lease = task.get("agent_stagnation_recovery_lease") if isinstance(task.get("agent_stagnation_recovery_lease"), dict) else {}
    if str(lease.get("status") or "") != "granted":
        return False
    if resilience.durable_state_key(task) != str(lease.get("state_key") or ""):
        return False
    if _as_int(task.get("agent_cycle")) <= _as_int(lease.get("granted_cycle")):
        return False
    consumed = {"value": False}

    def apply(latest: Dict[str, Any]) -> None:
        current = latest.get("agent_stagnation_recovery_lease") if isinstance(latest.get("agent_stagnation_recovery_lease"), dict) else {}
        if str(current.get("id") or "") != str(lease.get("id") or ""):
            return
        if str(current.get("status") or "") != "granted":
            return
        if resilience.durable_state_key(latest) != str(current.get("state_key") or ""):
            return
        progress = latest.get("agent_progress_state") if isinstance(latest.get("agent_progress_state"), dict) else {}
        progress = dict(progress)
        progress["stagnant_cycles"] = 0
        latest["agent_progress_state"] = progress
        controller = (
            latest.get("agent_stagnation_controller")
            if isinstance(latest.get("agent_stagnation_controller"), dict)
            else {}
        )
        if (
            str(controller.get("state_key") or "") == str(current.get("state_key") or "")
            and str(controller.get("run_id") or "") == str(latest.get("agent_run_id") or "")
        ):
            controller = dict(controller)
            controller.update({
                "last_cycle": _as_int(latest.get("agent_cycle")),
                "cycles": 0,
                "progress_stagnant_cycles": 0,
                "stage": "observe",
                "intervention_kind": "observe",
                "updated_at": time.time(),
            })
            latest["agent_stagnation_controller"] = controller
        current = dict(current)
        current.update({
            "remaining_transitions": 0,
            "status": "consumed",
            "consumed_cycle": _as_int(latest.get("agent_cycle")),
            "consumed_at": time.time(),
        })
        latest["agent_stagnation_recovery_lease"] = current
        events = latest.get("agent_events") if isinstance(latest.get("agent_events"), list) else []
        events.append({
            "type": "no_progress_recovery",
            "ts": int(time.time()),
            "cycle": latest.get("agent_cycle"),
            "state_key": current.get("state_key"),
            "recovery_kind": current.get("kind"),
            "summary": "Consumed one explicit state-keyed recovery credit; controller guidance itself was not counted as progress.",
        })
        latest["agent_events"] = events[-1000:]
        consumed["value"] = True

    cw.mutate_task(task_id, apply)
    return consumed["value"]


def process_task(task_id: str) -> bool:
    task = cw.load_task(task_id)
    if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
        return False
    if _consume_recovery_lease(task_id, task):
        task = cw.load_task(task_id)
    else:
        lease = (
            task.get("agent_stagnation_recovery_lease")
            if isinstance(task.get("agent_stagnation_recovery_lease"), dict)
            else {}
        )
        if (
            str(lease.get("status") or "") == "granted"
            and str(lease.get("kind") or "") == "continuation"
            and str(lease.get("state_key") or "") == resilience.durable_state_key(task)
            and str(lease.get("run_id") or "") == str(task.get("agent_run_id") or "")
            and _as_int(task.get("agent_cycle")) <= _as_int(lease.get("granted_cycle"))
        ):
            # The checkpoint that granted this lease has already been claimed
            # for the current sample. Do not immediately fall through from a
            # continuation checkpoint into a terminal-stage checkpoint.
            return False
    checkpoint = _prepare_checkpoint(task)
    kind = str(checkpoint.get("intervention_kind") or "observe")
    if kind == "observe":
        _persist_observation(task_id, checkpoint)
        return False
    intervention = str(checkpoint.get("intervention_id") or "")
    controller = checkpoint.get("controller") if isinstance(checkpoint.get("controller"), dict) else {}
    if resilience.intervention_already_claimed(controller, intervention):
        _persist_observation(task_id, checkpoint)
        return False
    return _claim_checkpoint(task_id, checkpoint)


def scan_once() -> Dict[str, Any]:
    processed: List[str] = []
    failures: Dict[str, str] = {}
    cw._ensure_dirs()
    for path in cw.tasks_dir().glob("code_*.json"):
        task_id = path.stem
        try:
            if process_task(task_id):
                processed.append(task_id)
        except Exception as exc:
            failures[task_id] = f"{type(exc).__name__}: {exc}"
    return {"ok": not failures, "processed": processed, "failures": failures}


async def _runtime_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(scan_once)
            if result.get("failures"):
                logger.warning("coding semantic memory scan failures=%s", result.get("failures"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("coding semantic memory scan failed (%s: %s)", type(exc).__name__, exc)
        await asyncio.sleep(_poll_interval())


async def start_runtime() -> None:
    global _RUNTIME_TASK
    if _RUNTIME_TASK is not None and not _RUNTIME_TASK.done():
        return
    _RUNTIME_TASK = asyncio.create_task(_runtime_loop(), name="coding-semantic-memory")


async def stop_runtime() -> None:
    global _RUNTIME_TASK
    task = _RUNTIME_TASK
    _RUNTIME_TASK = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
