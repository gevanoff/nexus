from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from app import coding_workspace as cw
from app.config import S, logger


_RUNTIME_TASK: Optional[asyncio.Task[Any]] = None
_ACTIVE_STATUSES = {"queued", "running", "stopping", "pausing"}
_INSPECTION_TOOLS = {
    "coding_search_text",
    "coding_read_file",
    "coding_read_file_lines",
    "coding_git_status",
    "coding_git_diff",
    "coding_change_summary",
}


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _poll_interval() -> float:
    try:
        return max(0.5, min(float(getattr(S, "CODING_SEMANTIC_MEMORY_POLL_SEC", 2.0) or 2.0), 30.0))
    except Exception:
        return 2.0


def _stagnation_threshold(task: Dict[str, Any]) -> int:
    mission = cw.normalize_coding_mission(task)
    budget = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
    maximum = max(2, _as_int(budget.get("max_no_progress_cycles") or 8))
    configured = _as_int(getattr(S, "CODING_SEMANTIC_MEMORY_STAGNANT_CYCLES", 0) or 0)
    if configured > 0:
        return max(1, min(configured, maximum - 1))
    return max(2, min(4, maximum // 2, maximum - 1))


def _current_run_events(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    run_id = str(task.get("agent_run_id") or "").strip()
    start_index = 0
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if str(event.get("type") or "") != "started":
            continue
        event_run_id = str(event.get("run_id") or "").strip()
        if not run_id or not event_run_id or event_run_id == run_id:
            start_index = index
            break
    return events[start_index:]


def _inspection_target(event: Dict[str, Any]) -> str:
    name = str(event.get("name") or "").strip()
    if name not in _INSPECTION_TOOLS:
        return ""
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    if name == "coding_search_text":
        path = str(args.get("path") or ".").strip() or "."
        query = _clip(args.get("query"), 120)
        return f"search {path}: {query}" if query else f"search {path}"
    if name in {"coding_read_file", "coding_read_file_lines"}:
        path = str(args.get("path") or "").strip()
        if not path:
            return name
        start = _as_int(args.get("start_line"))
        count = _as_int(args.get("line_count"))
        suffix = f" lines {start}-{start + count - 1}" if start > 0 and count > 0 else ""
        return f"read {path}{suffix}"
    return name.removeprefix("coding_")


def _dedupe_recent(values: List[str], *, limit: int) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in reversed(values):
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= limit:
            break
    output.reverse()
    return output


def build_investigation_checkpoint(task: Dict[str, Any]) -> Dict[str, Any]:
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    observation = progress.get("observation") if isinstance(progress.get("observation"), dict) else {}
    events = _current_run_events(task)
    inspected = _dedupe_recent(
        [_inspection_target(event) for event in events if str(event.get("type") or "") == "tool_started"],
        limit=16,
    )
    notes = _dedupe_recent(
        [
            _clip(event.get("content"), 600)
            for event in events
            if str(event.get("type") or "") == "assistant" and str(event.get("content") or "").strip()
        ],
        limit=3,
    )
    plan = cw.normalize_project_plan(task.get("project_plan"), fallback_goal=str(task.get("prompt") or ""))
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    active_item = next(
        (
            item
            for item in items
            if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "in_progress"
        ),
        None,
    )
    active_summary = ""
    if isinstance(active_item, dict):
        active_summary = _clip(
            f"{active_item.get('title') or ''}: {active_item.get('summary') or ''}",
            500,
        )
    state_key = json.dumps(
        {
            "workspace_fingerprint": str(observation.get("workspace_fingerprint") or ""),
            "plan_revision": _as_int(observation.get("plan_revision")),
            "validation_revision": _as_int(observation.get("validation_revision")),
            "diff_review_revision": _as_int(observation.get("diff_review_revision")),
            "finish_state": str(observation.get("finish_state") or "running"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema": "nexus_coding_investigation_checkpoint.v1",
        "run_id": str(task.get("agent_run_id") or ""),
        "generated_at": time.time(),
        "cycle": _as_int(task.get("agent_cycle")),
        "stagnant_cycles": _as_int(progress.get("stagnant_cycles")),
        "state_key": state_key,
        "inspected_targets": inspected,
        "unverified_model_notes": notes,
        "active_plan_item": active_summary,
        "unresolved_question": (
            "Which established finding justifies the smallest viable edit, or what concrete blocker prevents that edit?"
        ),
        "next_action": (
            "Stop broad orientation. Persist the established findings in the project plan, then make the smallest viable edit; "
            "otherwise call coding_finish with success=false and a concrete blocker."
        ),
    }


def render_checkpoint_guidance(checkpoint: Dict[str, Any]) -> str:
    inspected = checkpoint.get("inspected_targets") if isinstance(checkpoint.get("inspected_targets"), list) else []
    notes = checkpoint.get("unverified_model_notes") if isinstance(checkpoint.get("unverified_model_notes"), list) else []
    lines = [
        "Controller investigation checkpoint (durable across context compaction and continuation):",
        (
            f"- No durable state transition has occurred for {checkpoint.get('stagnant_cycles') or 0} cycles "
            f"as of cycle {checkpoint.get('cycle') or 0}."
        ),
    ]
    if checkpoint.get("active_plan_item"):
        lines.append(f"- Active project-plan item: {checkpoint['active_plan_item']}")
    if inspected:
        lines.append("- Already inspected; do not repeat unless directly required for the next edit:")
        lines.extend(f"  - {item}" for item in inspected)
    if notes:
        lines.append("- Recent model notes (unverified; confirm against repository evidence rather than treating them as facts):")
        lines.extend(f"  - {item}" for item in notes)
    lines.extend(
        [
            f"- Unresolved question: {checkpoint.get('unresolved_question') or ''}",
            f"- Required next action: {checkpoint.get('next_action') or ''}",
            "A note-only rewrite against the same repository state will not earn another recovery checkpoint.",
        ]
    )
    return "\n".join(lines)


def _claim_checkpoint(task_id: str, checkpoint: Dict[str, Any]) -> bool:
    claimed = {"value": False}
    state_key = str(checkpoint.get("state_key") or "")
    run_id = str(checkpoint.get("run_id") or "")

    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
            return
        if str(task.get("agent_run_id") or "") != run_id:
            return
        if str(task.get("agent_investigation_guidance_state_key") or "") == state_key:
            return
        task["agent_investigation_checkpoint"] = dict(checkpoint)
        task["agent_investigation_guidance_state_key"] = state_key
        events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
        events.append(
            {
                "type": "investigation_checkpoint",
                "ts": int(time.time()),
                "cycle": checkpoint.get("cycle"),
                "stagnant_cycles": checkpoint.get("stagnant_cycles"),
                "state_key": state_key,
                "summary": "Persisted a bounded investigation checkpoint before further broad inspection.",
            }
        )
        task["agent_events"] = events[-1000:]
        claimed["value"] = True

    cw.mutate_task(task_id, apply)
    return claimed["value"]


def _release_checkpoint_claim(task_id: str, state_key: str, error: str) -> None:
    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_investigation_guidance_state_key") or "") == state_key:
            task["agent_investigation_guidance_state_key"] = ""
            task["agent_investigation_checkpoint_error"] = _clip(error, 1000)

    cw.mutate_task(task_id, apply)


def process_task(task_id: str) -> bool:
    task = cw.load_task(task_id)
    if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
        return False
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    stagnant = _as_int(progress.get("stagnant_cycles"))
    if stagnant < _stagnation_threshold(task):
        return False
    checkpoint = build_investigation_checkpoint(task)
    if not _claim_checkpoint(task_id, checkpoint):
        return False
    state_key = str(checkpoint.get("state_key") or "")
    try:
        cw.append_guidance_message(
            task_id,
            message=render_checkpoint_guidance(checkpoint),
            actor="nexus-controller",
        )
    except Exception as exc:
        _release_checkpoint_claim(task_id, state_key, f"{type(exc).__name__}: {exc}")
        raise
    return True


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
