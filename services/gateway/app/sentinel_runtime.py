from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app import agent_tasks
from app.backends import get_admission_controller
from app.config import S
from app.openai_utils import new_id
from app.resources_snapshot import build_registry_backend_status_payload, call_lifecycle_manager, status_exception_text


log = logging.getLogger(__name__)

_TASK_LOOP: asyncio.Task | None = None
_STOP_EVENT: asyncio.Event | None = None
_RUNTIME_STATUS: Dict[str, Any] = {
    "running": False,
    "started_at": 0,
    "last_tick_started_at": 0,
    "last_tick_finished_at": 0,
    "last_error": "",
    "last_summary": {},
}


def _db_path() -> str:
    return (getattr(S, "NEXUS_SENTINEL_DB_PATH", "") or "/var/lib/gateway/data/sentinel/sentinel.sqlite").strip()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _now() -> int:
    return int(time.time())


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha1(_stable_json(value).encode("utf-8")).hexdigest()


def _max_events() -> int:
    try:
        return max(100, int(getattr(S, "NEXUS_SENTINEL_MAX_EVENTS", 5000) or 5000))
    except Exception:
        return 5000


def _poll_interval_sec() -> float:
    try:
        return max(2.0, float(getattr(S, "NEXUS_SENTINEL_POLL_INTERVAL_SEC", 15.0) or 15.0))
    except Exception:
        return 15.0


def _stalled_after_sec() -> float:
    try:
        return max(60.0, float(getattr(S, "NEXUS_SENTINEL_STALLED_AFTER_SEC", 900.0) or 900.0))
    except Exception:
        return 900.0


def _resource_pressure_pct() -> float:
    try:
        return max(0.5, min(0.99, float(getattr(S, "NEXUS_SENTINEL_RESOURCE_PRESSURE_PCT", 0.9) or 0.9)))
    except Exception:
        return 0.9


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sentinel_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_ts INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sentinel_events (
                id TEXT PRIMARY KEY,
                ts INTEGER NOT NULL,
                level TEXT NOT NULL,
                category TEXT NOT NULL,
                event_type TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_events_ts ON sentinel_events(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_events_category ON sentinel_events(category, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_events_fingerprint ON sentinel_events(fingerprint, ts DESC)")


def _load_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute("SELECT value_json FROM sentinel_state WHERE key='runtime_state'").fetchone()
    if row is None:
        return {}
    try:
        parsed = json.loads(row["value_json"] or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _save_state(conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO sentinel_state(key, value_json, updated_ts)
        VALUES('runtime_state', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_ts=excluded.updated_ts
        """,
        (_stable_json(state), _now()),
    )


def _record_event(
    conn: sqlite3.Connection,
    *,
    ts: int,
    level: str,
    category: str,
    event_type: str,
    subject_type: str,
    subject_id: str,
    title: str,
    summary: str,
    reasoning: str,
    dedupe_key: str,
    fingerprint: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "id": new_id("sentinel"),
        "ts": int(ts),
        "level": str(level or "info"),
        "category": str(category or "general"),
        "event_type": str(event_type or "note"),
        "subject_type": str(subject_type or "system"),
        "subject_id": str(subject_id or "global"),
        "title": str(title or "Nexus Sentinel event")[:200],
        "summary": str(summary or "")[:800],
        "reasoning": str(reasoning or "")[:5000],
        "dedupe_key": str(dedupe_key or "")[:200],
        "fingerprint": str(fingerprint or "")[:64],
        "details": details if isinstance(details, dict) else {},
    }
    conn.execute(
        """
        INSERT INTO sentinel_events(
            id, ts, level, category, event_type, subject_type, subject_id,
            title, summary, reasoning, dedupe_key, fingerprint, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["id"],
            payload["ts"],
            payload["level"],
            payload["category"],
            payload["event_type"],
            payload["subject_type"],
            payload["subject_id"],
            payload["title"],
            payload["summary"],
            payload["reasoning"],
            payload["dedupe_key"],
            payload["fingerprint"],
            _stable_json(payload["details"]),
        ),
    )
    excess = conn.execute("SELECT COUNT(*) AS count FROM sentinel_events").fetchone()
    total = int(excess["count"] or 0) if excess is not None else 0
    overflow = total - _max_events()
    if overflow > 0:
        conn.execute(
            "DELETE FROM sentinel_events WHERE id IN (SELECT id FROM sentinel_events ORDER BY ts ASC LIMIT ?)",
            (overflow,),
        )
    return payload


def _trim_bucket(bucket: Any, *, limit: int = 500) -> Dict[str, Any]:
    if not isinstance(bucket, dict):
        return {}
    items = list(bucket.items())
    if len(items) <= limit:
        return {str(key): value for key, value in items}

    def _score(value: Any) -> int:
        if isinstance(value, dict):
            for key in ("last_sent_ts", "last_resume_ts", "ts"):
                try:
                    return int(value.get(key) or 0)
                except Exception:
                    continue
        try:
            return int(value or 0)
        except Exception:
            return 0

    items.sort(key=lambda item: _score(item[1]), reverse=True)
    return {str(key): value for key, value in items[:limit]}


def _event_rows_to_payload(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except Exception:
            details = {}
        out.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "level": row["level"],
                "category": row["category"],
                "event_type": row["event_type"],
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "title": row["title"],
                "summary": row["summary"],
                "reasoning": row["reasoning"],
                "dedupe_key": row["dedupe_key"],
                "fingerprint": row["fingerprint"],
                "details": details if isinstance(details, dict) else {},
            }
        )
    return out


def list_events(*, limit: int = 100, category: str = "", level: str = "") -> List[Dict[str, Any]]:
    init_db()
    cap = max(1, min(int(limit or 100), 500))
    clauses: List[str] = []
    params: List[Any] = []
    if str(category or "").strip():
        clauses.append("category = ?")
        params.append(str(category).strip())
    if str(level or "").strip():
        clauses.append("level = ?")
        params.append(str(level).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM sentinel_events {where} ORDER BY ts DESC LIMIT ?",
            [*params, cap],
        ).fetchall()
    return _event_rows_to_payload(list(rows))


def recurring_issues(*, limit: int = 20, since_sec: int = 7 * 24 * 3600) -> List[Dict[str, Any]]:
    init_db()
    cap = max(1, min(int(limit or 20), 100))
    cutoff = _now() - max(60, int(since_sec or 0))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                category,
                event_type,
                fingerprint,
                MAX(title) AS title,
                MAX(summary) AS summary,
                COUNT(*) AS hit_count,
                MAX(ts) AS last_seen_ts,
                MAX(subject_type) AS subject_type,
                MAX(subject_id) AS subject_id
            FROM sentinel_events
            WHERE ts >= ? AND fingerprint <> ''
            GROUP BY category, event_type, fingerprint
            ORDER BY hit_count DESC, last_seen_ts DESC
            LIMIT ?
            """,
            (cutoff, cap),
        ).fetchall()
    return [
        {
            "category": row["category"],
            "event_type": row["event_type"],
            "fingerprint": row["fingerprint"],
            "title": row["title"],
            "summary": row["summary"],
            "hit_count": int(row["hit_count"] or 0),
            "last_seen_ts": int(row["last_seen_ts"] or 0),
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
        }
        for row in rows
    ]


def status_payload(*, limit: int = 120) -> Dict[str, Any]:
    return {
        "runtime": dict(_RUNTIME_STATUS),
        "events": list_events(limit=limit),
        "recurring": recurring_issues(),
    }


def _format_recent_event_lines(events: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for item in events[:6]:
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        event_type = str(item.get("type") or "").strip() or "event"
        lines.append(f"- {event_type}: {summary}")
    return "\n".join(lines)


def _coding_reasoning(item: Dict[str, Any]) -> str:
    agent = item.get("agent") if isinstance(item.get("agent"), dict) else {}
    lines = [
        f"Workspace status: {item.get('status') or 'unknown'}",
        f"Agent status: {agent.get('status') or 'unknown'}",
        f"Attention flags: {', '.join(item.get('attention') or []) or 'none'}",
        f"Recommended action: {item.get('recommended_action') or 'none'}",
        f"Safe actions: {', '.join(item.get('safe_actions') or []) or 'none'}",
    ]
    last_age = agent.get("last_event_age_sec")
    if last_age is not None:
        lines.append(f"Last agent event age: {last_age}s")
    no_tool = item.get("no_tool_call_streak")
    if no_tool:
        lines.append(f"No-tool-call streak: {no_tool}")
    repo_error = str(item.get("repo_error") or "").strip()
    if repo_error:
        lines.append(f"Repository inspection error: {repo_error}")
    recent = item.get("recent_events") if isinstance(item.get("recent_events"), list) else []
    recent_lines = _format_recent_event_lines(recent)
    if recent_lines:
        lines.append("Recent agent events:\n" + recent_lines)
    return "\n".join(lines)


async def _maybe_notify_coding(
    state: Dict[str, Any],
    *,
    item: Dict[str, Any],
    event_kind: str,
    fingerprint: str,
    action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from app import telegram_notifications

    task_id = str(item.get("id") or "")
    notifications = state.setdefault("coding_notifications", {})
    key = f"{task_id}:{event_kind}"
    previous = notifications.get(key) if isinstance(notifications.get(key), dict) else {}
    cooldown_sec = max(0, int(getattr(S, "NEXUS_SENTINEL_NOTIFICATION_COOLDOWN_SEC", 6 * 60 * 60) or 6 * 60 * 60))
    now = _now()
    last_sent_ts = int(previous.get("last_sent_ts") or 0)
    if previous.get("fingerprint") == fingerprint and cooldown_sec > 0 and last_sent_ts > 0 and (now - last_sent_ts) < cooldown_sec:
        return {"sent": False, "reason": "cooldown"}

    target = telegram_notifications.resolve_notification_target(
        user_id=item.get("owner_user_id"),
        owner_username=item.get("owner"),
        app="coding",
    )
    if not bool(target.get("enabled")):
        return {"sent": False, "reason": str(target.get("reason") or "disabled")}
    if event_kind == "auto_resume" and not bool(target.get("notify_on_recovery")):
        return {"sent": False, "reason": "recovery_disabled"}
    if event_kind == "needs_attention" and not bool(target.get("notify_on_attention")):
        return {"sent": False, "reason": "attention_disabled"}

    text = telegram_notifications.render_coding_workspace_notification(
        item=item,
        event_kind=event_kind,
        mention_username=str(target.get("mention_username") or ""),
        action=action,
    )
    result = await telegram_notifications.send_message(chat_id=str(target.get("chat_id") or ""), text=text)
    if bool(result.get("ok")):
        notifications[key] = {"fingerprint": fingerprint, "last_sent_ts": now}
        state["coding_notifications"] = _trim_bucket(notifications, limit=1000)
        return {"sent": True, "chat_id": str(target.get("chat_id") or "")}
    return {"sent": False, "reason": str(result.get("error") or "send_failed")}


async def _monitor_coding(state: Dict[str, Any], conn: sqlite3.Connection, *, now: int) -> Dict[str, Any]:
    from app import coding_agent, coding_workspace

    monitor = coding_workspace.monitor_tasks(limit=100, only_attention=False, stalled_after_sec=_stalled_after_sec())
    items = monitor.get("tasks") if isinstance(monitor.get("tasks"), list) else []
    summary = {
        "total": int(((monitor.get("counts") or {}).get("total") or 0)),
        "attention": int(((monitor.get("counts") or {}).get("attention") or 0)),
        "actions": 0,
        "notifications": 0,
    }
    previous_active = state.get("coding_active") if isinstance(state.get("coding_active"), dict) else {}
    current_active: Dict[str, Any] = {}
    attempts = state.get("coding_resume_attempts") if isinstance(state.get("coding_resume_attempts"), dict) else {}
    cooldown_sec = max(0, int(getattr(S, "NEXUS_SENTINEL_RESUME_COOLDOWN_SEC", 1800) or 1800))

    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "").strip()
        if not task_id:
            continue
        needs_attention = bool(item.get("needs_attention"))
        attention = item.get("attention") if isinstance(item.get("attention"), list) else []
        fingerprint = _fingerprint(
            {
                "attention": attention,
                "agent_status": ((item.get("agent") or {}).get("status") if isinstance(item.get("agent"), dict) else ""),
                "recommended_action": item.get("recommended_action"),
            }
        )
        if needs_attention:
            current_active[task_id] = {"fingerprint": fingerprint, "attention": attention}
            previous = previous_active.get(task_id) if isinstance(previous_active.get(task_id), dict) else {}
            if previous.get("fingerprint") != fingerprint:
                _record_event(
                    conn,
                    ts=now,
                    level="warn",
                    category="coding",
                    event_type="needs_attention",
                    subject_type="coding_task",
                    subject_id=task_id,
                    title=f"Coding workspace needs attention: {task_id}",
                    summary=", ".join(attention) or "Coding workspace needs attention",
                    reasoning=_coding_reasoning(item),
                    dedupe_key=f"coding:{task_id}:attention",
                    fingerprint=fingerprint,
                    details={"item": item},
                )
                notify_result = await _maybe_notify_coding(state, item=item, event_kind="needs_attention", fingerprint=fingerprint)
                if notify_result.get("sent"):
                    summary["notifications"] += 1

        safe_actions = item.get("safe_actions") if isinstance(item.get("safe_actions"), list) else []
        agent = item.get("agent") if isinstance(item.get("agent"), dict) else {}
        agent_status = str(agent.get("status") or "").strip().lower()
        can_resume = agent_status in {"paused", "stopped", "interrupted", "failed"} and ("resume" in safe_actions or "guide_and_resume" in safe_actions)
        if not can_resume:
            continue
        previous_attempt = attempts.get(task_id) if isinstance(attempts.get(task_id), dict) else {}
        last_resume_ts = int(previous_attempt.get("last_resume_ts") or 0)
        if cooldown_sec > 0 and last_resume_ts > 0 and (now - last_resume_ts) < cooldown_sec:
            continue

        updated = await coding_agent.start_agent_run(task_id, actor="nexus-sentinel-auto")
        next_agent = updated.get("agent") if isinstance(updated.get("agent"), dict) else {}
        action = {
            "task_id": task_id,
            "action": "resume",
            "previous_status": agent_status,
            "agent_status": str(next_agent.get("status") or ""),
        }
        attempts[task_id] = {
            "last_resume_ts": now,
            "attempt_count": int(previous_attempt.get("attempt_count") or 0) + 1,
            "last_status": agent_status,
        }
        summary["actions"] += 1
        action_fingerprint = _fingerprint({"task_id": task_id, "action": action})
        _record_event(
            conn,
            ts=now,
            level="info",
            category="coding",
            event_type="auto_resume",
            subject_type="coding_task",
            subject_id=task_id,
            title=f"Nexus Sentinel auto-resumed {task_id}",
            summary=f"{agent_status} -> {action['agent_status'] or 'queued'}",
            reasoning=_coding_reasoning(item),
            dedupe_key=f"coding:{task_id}:auto_resume",
            fingerprint=action_fingerprint,
            details={"item": item, "action": action},
        )
        notify_result = await _maybe_notify_coding(state, item=item, event_kind="auto_resume", fingerprint=action_fingerprint, action=action)
        if notify_result.get("sent"):
            summary["notifications"] += 1

    for task_id, previous in previous_active.items():
        if task_id in current_active:
            continue
        fingerprint = str((previous or {}).get("fingerprint") or "")
        _record_event(
            conn,
            ts=now,
            level="info",
            category="coding",
            event_type="resolved",
            subject_type="coding_task",
            subject_id=task_id,
            title=f"Coding workspace attention cleared: {task_id}",
            summary="Previously flagged coding workspace issue is no longer active.",
            reasoning="The workspace is no longer reporting attention flags in the current Sentinel monitor pass.",
            dedupe_key=f"coding:{task_id}:resolved",
            fingerprint=fingerprint,
            details={},
        )

    state["coding_active"] = current_active
    state["coding_resume_attempts"] = _trim_bucket(attempts, limit=500)
    return summary


def _agent_tasks_connection() -> sqlite3.Connection:
    path = (getattr(S, "AGENT_TASKS_DB_PATH", "") or "").strip()
    if not path:
        raise RuntimeError("AGENT_TASKS_DB_PATH not configured")
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _monitor_scheduled_tasks(state: Dict[str, Any], conn: sqlite3.Connection, *, now: int) -> Dict[str, Any]:
    init_db()
    summary = {"completed": 0, "failed": 0, "overdue": 0}
    seen_runs = state.get("scheduled_seen_runs") if isinstance(state.get("scheduled_seen_runs"), dict) else {}
    previous_overdue = state.get("scheduled_overdue") if isinstance(state.get("scheduled_overdue"), dict) else {}
    current_overdue: Dict[str, Any] = {}

    try:
        with _agent_tasks_connection() as task_conn:
            tasks = task_conn.execute("SELECT * FROM agent_tasks ORDER BY updated_ts DESC LIMIT 300").fetchall()
    except Exception as exc:
        _record_event(
            conn,
            ts=now,
            level="error",
            category="scheduled_tasks",
            event_type="monitor_error",
            subject_type="scheduler",
            subject_id="agent_tasks",
            title="Scheduled task monitor failed",
            summary=str(exc),
            reasoning=f"Could not read the scheduled task database: {type(exc).__name__}: {exc}",
            dedupe_key="scheduled:monitor_error",
            fingerprint=_fingerprint(str(exc)),
            details={},
        )
        return summary

    bootstrap = not bool(seen_runs)
    for row in tasks:
        task = agent_tasks._task_row_to_dict(row)
        task_id = str(task.get("id") or "")
        last_run_id = str(task.get("last_run_id") or "")
        if not task_id:
            continue
        if last_run_id:
            previous = str(seen_runs.get(task_id) or "")
            if not bootstrap and last_run_id != previous:
                ok = task.get("last_ok") is True
                event_type = "completed" if ok else "failed"
                level = "info" if ok else "error"
                title = f"Scheduled task {event_type}: {task.get('title') or task_id}"
                summary_text = "Run completed successfully." if ok else str(task.get("last_error") or "Run failed")
                reasoning = (
                    f"Task id: {task_id}\n"
                    f"Kind: {task.get('kind') or 'unknown'}\n"
                    f"Status after run: {task.get('status') or 'unknown'}\n"
                    f"Run count: {task.get('run_count') or 0}\n"
                    f"Last error: {task.get('last_error') or 'none'}"
                )
                _record_event(
                    conn,
                    ts=now,
                    level=level,
                    category="scheduled_tasks",
                    event_type=event_type,
                    subject_type="agent_task",
                    subject_id=task_id,
                    title=title,
                    summary=summary_text,
                    reasoning=reasoning,
                    dedupe_key=f"scheduled:{task_id}:{event_type}",
                    fingerprint=_fingerprint({"run": last_run_id, "ok": task.get('last_ok'), "error": task.get('last_error')}),
                    details={"task": task},
                )
                summary["completed" if ok else "failed"] += 1
            seen_runs[task_id] = last_run_id

        next_run_ts = int(task.get("next_run_ts") or 0)
        status = str(task.get("status") or "")
        if status == "enabled" and next_run_ts > 0 and next_run_ts < (now - 60):
            fingerprint = _fingerprint({"next_run_ts": next_run_ts, "status": status})
            current_overdue[task_id] = {"fingerprint": fingerprint, "next_run_ts": next_run_ts}
            previous = previous_overdue.get(task_id) if isinstance(previous_overdue.get(task_id), dict) else {}
            if previous.get("fingerprint") != fingerprint:
                _record_event(
                    conn,
                    ts=now,
                    level="warn",
                    category="scheduled_tasks",
                    event_type="overdue",
                    subject_type="agent_task",
                    subject_id=task_id,
                    title=f"Scheduled task appears overdue: {task.get('title') or task_id}",
                    summary=f"Next run was due at {next_run_ts} and the task is still enabled.",
                    reasoning=(
                        f"Task id: {task_id}\n"
                        f"Kind: {task.get('kind') or 'unknown'}\n"
                        f"Status: {status}\n"
                        f"Next run ts: {next_run_ts}\n"
                        f"Last run ts: {task.get('last_run_ts') or 0}"
                    ),
                    dedupe_key=f"scheduled:{task_id}:overdue",
                    fingerprint=fingerprint,
                    details={"task": task},
                )
            summary["overdue"] += 1

    for task_id, previous in previous_overdue.items():
        if task_id in current_overdue:
            continue
        _record_event(
            conn,
            ts=now,
            level="info",
            category="scheduled_tasks",
            event_type="resolved",
            subject_type="agent_task",
            subject_id=task_id,
            title=f"Scheduled task backlog cleared: {task_id}",
            summary="Previously overdue scheduled task is no longer behind.",
            reasoning="The task is no longer detected as overdue in the current Sentinel pass.",
            dedupe_key=f"scheduled:{task_id}:resolved",
            fingerprint=str((previous or {}).get("fingerprint") or ""),
            details={},
        )

    state["scheduled_seen_runs"] = _trim_bucket(seen_runs, limit=1000)
    state["scheduled_overdue"] = current_overdue
    return summary


async def _build_resource_payload() -> Dict[str, Any]:
    payload = await build_registry_backend_status_payload()
    try:
        lifecycle = await call_lifecycle_manager("GET", "/v1/lifecycle/status", timeout=10.0)
        if isinstance(lifecycle, dict):
            payload["hosts"] = lifecycle.get("hosts") if isinstance(lifecycle.get("hosts"), list) else []
            if isinstance(lifecycle.get("core_services"), list):
                payload["core_services"] = lifecycle.get("core_services")
    except Exception as exc:
        payload.setdefault("errors", []).append(status_exception_text(exc))
    return payload


async def _monitor_resources(state: Dict[str, Any], conn: sqlite3.Connection, *, now: int) -> Dict[str, Any]:
    summary = {"backend_issues": 0, "resource_pressure": 0, "queue_pressure": 0}
    previous_backend = state.get("backend_issues") if isinstance(state.get("backend_issues"), dict) else {}
    current_backend: Dict[str, Any] = {}
    previous_resource = state.get("resource_issues") if isinstance(state.get("resource_issues"), dict) else {}
    current_resource: Dict[str, Any] = {}
    previous_queue = state.get("queue_issues") if isinstance(state.get("queue_issues"), dict) else {}
    current_queue: Dict[str, Any] = {}

    payload = await _build_resource_payload()
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    for raw_error in errors:
        error_text = str(raw_error or "").strip()
        if not error_text:
            continue
        fingerprint = _fingerprint(error_text)
        current_backend["resource_payload_error"] = {"fingerprint": fingerprint}
        if previous_backend.get("resource_payload_error", {}).get("fingerprint") != fingerprint:
            _record_event(
                conn,
                ts=now,
                level="error",
                category="resources",
                event_type="monitor_error",
                subject_type="system",
                subject_id="resources",
                title="Resource monitor degraded",
                summary=error_text,
                reasoning="The Sentinel resource monitor could not fully assemble the lifecycle/resources payload.",
                dedupe_key="resources:monitor_error",
                fingerprint=fingerprint,
                details={"errors": errors},
            )

    backends = payload.get("backends") if isinstance(payload.get("backends"), list) else []
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        backend_class = str(backend.get("backend_class") or "").strip()
        if not backend_class:
            continue
        issue_bits: List[str] = []
        if backend.get("active") and backend.get("ready") is False:
            issue_bits.append("active_not_ready")
        if backend.get("active") and backend.get("healthy") is False:
            issue_bits.append("active_unhealthy")
        if str(backend.get("status") or "").strip().lower() in {"inactive_unhealthy", "error"}:
            issue_bits.append(str(backend.get("status") or ""))
        last_checked_at = int(backend.get("last_checked_at") or backend.get("last_check") or 0)
        poll_window = int(max(15.0, _poll_interval_sec() * 4))
        if last_checked_at > 0 and (now - last_checked_at) > poll_window:
            issue_bits.append("stale")
        if not issue_bits:
            continue
        fingerprint = _fingerprint({"issues": issue_bits, "error": backend.get("error"), "status": backend.get("status")})
        current_backend[backend_class] = {"fingerprint": fingerprint}
        if previous_backend.get(backend_class, {}).get("fingerprint") != fingerprint:
            _record_event(
                conn,
                ts=now,
                level="warn",
                category="resources",
                event_type="backend_issue",
                subject_type="backend",
                subject_id=backend_class,
                title=f"Backend issue detected: {backend_class}",
                summary=", ".join(issue_bits),
                reasoning=(
                    f"Backend status: {backend.get('status') or 'unknown'}\n"
                    f"Active: {bool(backend.get('active'))}\n"
                    f"Healthy: {backend.get('healthy')}\n"
                    f"Ready: {backend.get('ready')}\n"
                    f"Last check: {last_checked_at}\n"
                    f"Error: {backend.get('error') or backend.get('health_error') or 'none'}"
                ),
                dedupe_key=f"backend:{backend_class}:issue",
                fingerprint=fingerprint,
                details={"backend": backend},
            )
        summary["backend_issues"] += 1

    for backend_class, previous in previous_backend.items():
        if backend_class in current_backend:
            continue
        _record_event(
            conn,
            ts=now,
            level="info",
            category="resources",
            event_type="resolved",
            subject_type="backend",
            subject_id=backend_class,
            title=f"Backend issue cleared: {backend_class}",
            summary="Previously flagged backend issue is no longer active.",
            reasoning="The backend no longer matches the Sentinel issue signature in the current pass.",
            dedupe_key=f"backend:{backend_class}:resolved",
            fingerprint=str((previous or {}).get("fingerprint") or ""),
            details={},
        )

    threshold = _resource_pressure_pct()
    hosts = payload.get("hosts") if isinstance(payload.get("hosts"), list) else []
    for host in hosts:
        if not isinstance(host, dict):
            continue
        host_name = str(host.get("name") or host.get("hostname") or "").strip()
        if not host_name:
            continue
        issues: List[str] = []
        memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
        total_mb = float(memory.get("total_mb") or 0)
        available_mb = float(memory.get("available_mb") or 0)
        used_pct = ((total_mb - available_mb) / total_mb) if total_mb > 0 else 0.0
        if total_mb > 0 and used_pct >= threshold:
            issues.append(f"ram_{int(round(used_pct * 100))}pct")
        gpus = host.get("gpus") if isinstance(host.get("gpus"), list) else []
        for gpu in gpus:
            if not isinstance(gpu, dict):
                continue
            gpu_total = float(gpu.get("memory_total_mb") or 0)
            gpu_used = float(gpu.get("memory_used_mb") or 0)
            gpu_pct = (gpu_used / gpu_total) if gpu_total > 0 else 0.0
            if gpu_total > 0 and gpu_pct >= threshold:
                issues.append(f"gpu_{gpu.get('index')}_{int(round(gpu_pct * 100))}pct")
        if not issues:
            continue
        fingerprint = _fingerprint(issues)
        current_resource[host_name] = {"fingerprint": fingerprint}
        if previous_resource.get(host_name, {}).get("fingerprint") != fingerprint:
            _record_event(
                conn,
                ts=now,
                level="warn",
                category="resources",
                event_type="resource_pressure",
                subject_type="host",
                subject_id=host_name,
                title=f"Resource pressure on {host_name}",
                summary=", ".join(issues),
                reasoning=(
                    f"Host: {host_name}\n"
                    f"RAM used: {int(round(used_pct * 100)) if total_mb > 0 else 0}%\n"
                    f"GPU pressure flags: {', '.join(issues) or 'none'}"
                ),
                dedupe_key=f"host:{host_name}:pressure",
                fingerprint=fingerprint,
                details={"host": host},
            )
        summary["resource_pressure"] += 1

    for host_name, previous in previous_resource.items():
        if host_name in current_resource:
            continue
        _record_event(
            conn,
            ts=now,
            level="info",
            category="resources",
            event_type="resolved",
            subject_type="host",
            subject_id=host_name,
            title=f"Resource pressure cleared on {host_name}",
            summary="Host resource pressure is below the Sentinel threshold again.",
            reasoning="The host no longer matches the Sentinel resource-pressure signature in the current pass.",
            dedupe_key=f"host:{host_name}:resolved",
            fingerprint=str((previous or {}).get("fingerprint") or ""),
            details={},
        )

    try:
        admission_stats = get_admission_controller().get_stats()
    except Exception as exc:
        admission_stats = {}
        fingerprint = _fingerprint(str(exc))
        current_queue["admission_error"] = {"fingerprint": fingerprint}
        if previous_queue.get("admission_error", {}).get("fingerprint") != fingerprint:
            _record_event(
                conn,
                ts=now,
                level="error",
                category="resources",
                event_type="admission_monitor_error",
                subject_type="system",
                subject_id="admission",
                title="Admission monitor failed",
                summary=str(exc),
                reasoning=f"Could not read admission controller statistics: {type(exc).__name__}: {exc}",
                dedupe_key="resources:admission_error",
                fingerprint=fingerprint,
                details={},
            )
    for key, stats in admission_stats.items():
        if not isinstance(stats, dict):
            continue
        limit = int(stats.get("limit") or 0)
        inflight = int(stats.get("inflight") or 0)
        if limit <= 0 or inflight < limit:
            continue
        fingerprint = _fingerprint(stats)
        current_queue[key] = {"fingerprint": fingerprint}
        if previous_queue.get(key, {}).get("fingerprint") != fingerprint:
            _record_event(
                conn,
                ts=now,
                level="warn",
                category="resources",
                event_type="queue_pressure",
                subject_type="admission",
                subject_id=key,
                title=f"Admission saturation: {key}",
                summary=f"Inflight {inflight} / limit {limit}",
                reasoning=f"The admission controller reports full utilization for {key}. Available slots: {stats.get('available') or 0}.",
                dedupe_key=f"admission:{key}:pressure",
                fingerprint=fingerprint,
                details={"stats": stats},
            )
        summary["queue_pressure"] += 1

    for key, previous in previous_queue.items():
        if key in current_queue:
            continue
        _record_event(
            conn,
            ts=now,
            level="info",
            category="resources",
            event_type="resolved",
            subject_type="admission",
            subject_id=key,
            title=f"Admission saturation cleared: {key}",
            summary="Admission controller utilization is below full saturation again.",
            reasoning="The admission controller no longer reports this route/backend pair at full capacity.",
            dedupe_key=f"admission:{key}:resolved",
            fingerprint=str((previous or {}).get("fingerprint") or ""),
            details={},
        )

    state["backend_issues"] = current_backend
    state["resource_issues"] = current_resource
    state["queue_issues"] = current_queue
    return summary


async def run_monitor_once() -> Dict[str, Any]:
    init_db()
    started = _now()
    _RUNTIME_STATUS["last_tick_started_at"] = started
    _RUNTIME_STATUS["last_error"] = ""
    with _connect() as conn:
        state = _load_state(conn)
        summary = {
            "coding": await _monitor_coding(state, conn, now=started),
            "scheduled_tasks": _monitor_scheduled_tasks(state, conn, now=started),
            "resources": await _monitor_resources(state, conn, now=started),
        }
        _save_state(conn, state)
    finished = _now()
    _RUNTIME_STATUS["last_tick_finished_at"] = finished
    _RUNTIME_STATUS["last_summary"] = summary
    return {"ok": True, "started_at": started, "finished_at": finished, "summary": summary}


async def _loop() -> None:
    init_db()
    stop = _STOP_EVENT
    _RUNTIME_STATUS["running"] = True
    _RUNTIME_STATUS["started_at"] = _now()
    while stop is not None and not stop.is_set():
        try:
            await run_monitor_once()
        except Exception as exc:
            _RUNTIME_STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
            log.exception("nexus sentinel tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=_poll_interval_sec())
        except asyncio.TimeoutError:
            pass
    _RUNTIME_STATUS["running"] = False


async def start_runtime() -> None:
    global _TASK_LOOP, _STOP_EVENT
    if not bool(getattr(S, "NEXUS_SENTINEL_ENABLED", True)):
        return
    init_db()
    if _TASK_LOOP is not None and not _TASK_LOOP.done():
        return
    _STOP_EVENT = asyncio.Event()
    _TASK_LOOP = asyncio.create_task(_loop())


async def stop_runtime() -> None:
    global _TASK_LOOP, _STOP_EVENT
    if _STOP_EVENT is not None:
        _STOP_EVENT.set()
    if _TASK_LOOP is not None:
        _TASK_LOOP.cancel()
        try:
            await _TASK_LOOP
        except asyncio.CancelledError:
            pass
    _TASK_LOOP = None
    _STOP_EVENT = None
    _RUNTIME_STATUS["running"] = False
