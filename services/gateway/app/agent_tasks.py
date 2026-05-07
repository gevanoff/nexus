from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import S
from app.models import AgentRunRequest
from app.openai_utils import new_id


log = logging.getLogger(__name__)


_TASK_LOOP: asyncio.Task | None = None
_STOP_EVENT: asyncio.Event | None = None


def _db_path() -> str:
    return (getattr(S, "AGENT_TASKS_DB_PATH", "") or "/var/lib/gateway/data/agent/tasks.sqlite").strip()


def _now() -> int:
    return int(time.time())


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                agent TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                run_at_ts INTEGER,
                interval_sec INTEGER,
                cron_expr TEXT,
                next_run_ts INTEGER,
                last_run_ts INTEGER,
                last_run_id TEXT,
                last_ok INTEGER,
                last_error TEXT,
                run_count INTEGER NOT NULL DEFAULT 0,
                max_runs INTEGER,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_task_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                due_ts INTEGER NOT NULL,
                started_ts INTEGER NOT NULL,
                finished_ts INTEGER,
                agent_run_id TEXT,
                ok INTEGER,
                output_text TEXT,
                error TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_due ON agent_tasks(status, next_run_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_task_runs_task ON agent_task_runs(task_id, started_ts)")
        conn.execute(
            "UPDATE agent_tasks SET status='enabled', updated_ts=? WHERE status='running'",
            (_now(),),
        )


def _parse_run_at(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError("run_at must be a positive Unix timestamp")
        return int(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_at must be an ISO-8601 string or Unix timestamp")
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError("run_at must be an ISO-8601 string or Unix timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def _positive_int(value: Any, name: str, *, min_value: int = 1, max_value: int = 2_147_483_647) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        out = int(value)
    except Exception as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if out < min_value:
        raise ValueError(f"{name} must be >= {min_value}")
    if out > max_value:
        raise ValueError(f"{name} must be <= {max_value}")
    return out


def _parse_cron_field(raw: str, *, min_value: int, max_value: int, dow: bool = False) -> set[int]:
    vals: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field part")
        step = 1
        if "/" in part:
            base, step_raw = part.split("/", 1)
            part = base.strip() or "*"
            step = _positive_int(step_raw.strip(), "cron step", min_value=1, max_value=max_value + 1) or 1
        if part == "*":
            start, end = min_value, max_value
        elif "-" in part:
            a, b = part.split("-", 1)
            start = int(a)
            end = int(b)
        else:
            start = end = int(part)
        if dow:
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        if start > end and not dow:
            raise ValueError("cron range start must be <= end")
        if dow and start > end:
            seq = list(range(start, max_value + 1)) + list(range(min_value, end + 1))
        else:
            seq = list(range(start, end + 1))
        for value in seq[::step]:
            if value < min_value or value > max_value:
                raise ValueError(f"cron value {value} outside {min_value}-{max_value}")
            vals.add(value)
    return vals


def _next_cron_ts(expr: str, after_ts: int) -> int:
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise ValueError("cron must have five fields: minute hour day month weekday")
    minutes = _parse_cron_field(parts[0], min_value=0, max_value=59)
    hours = _parse_cron_field(parts[1], min_value=0, max_value=23)
    days = _parse_cron_field(parts[2], min_value=1, max_value=31)
    months = _parse_cron_field(parts[3], min_value=1, max_value=12)
    weekdays = _parse_cron_field(parts[4], min_value=0, max_value=6, dow=True)

    # Search minute-by-minute. This is small, deterministic, and avoids adding a cron dependency.
    dt = datetime.fromtimestamp(after_ts + 60, timezone.utc).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        # Python Monday=0; cron commonly Sunday=0/7.
        cron_dow = (dt.weekday() + 1) % 7
        if (
            dt.minute in minutes
            and dt.hour in hours
            and dt.day in days
            and dt.month in months
            and cron_dow in weekdays
        ):
            return int(dt.timestamp())
        dt = dt + timedelta(minutes=1)
    raise ValueError("cron expression produced no run time within one year")


def _min_delay_sec() -> int:
    try:
        return max(0, int(getattr(S, "AGENT_TASKS_MIN_DELAY_SEC", 5) or 0))
    except Exception:
        return 5


def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        parsed = json.loads(row["metadata_json"] or "{}")
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:
        meta = {}
    return {
        "id": row["id"],
        "title": row["title"],
        "prompt": row["prompt"],
        "agent": row["agent"],
        "kind": row["kind"],
        "status": row["status"],
        "run_at_ts": row["run_at_ts"],
        "run_at": _iso(row["run_at_ts"]),
        "interval_sec": row["interval_sec"],
        "cron": row["cron_expr"],
        "next_run_ts": row["next_run_ts"],
        "next_run_at": _iso(row["next_run_ts"]),
        "last_run_ts": row["last_run_ts"],
        "last_run_at": _iso(row["last_run_ts"]),
        "last_run_id": row["last_run_id"],
        "last_ok": None if row["last_ok"] is None else bool(row["last_ok"]),
        "last_error": row["last_error"],
        "run_count": row["run_count"],
        "max_runs": row["max_runs"],
        "created_ts": row["created_ts"],
        "created_at": _iso(row["created_ts"]),
        "updated_ts": row["updated_ts"],
        "updated_at": _iso(row["updated_ts"]),
        "metadata": meta,
    }


def create_task(args: dict[str, Any]) -> dict[str, Any]:
    if not getattr(S, "AGENT_TASKS_ENABLED", True):
        return {"ok": False, "error": "agent task scheduler disabled"}
    init_db()

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "prompt must be a non-empty string"}
    prompt = prompt.strip()
    if len(prompt) > 20_000:
        return {"ok": False, "error": "prompt is too long"}

    title = args.get("title")
    if title is None:
        title = prompt[:80]
    if not isinstance(title, str) or not title.strip():
        return {"ok": False, "error": "title must be a non-empty string"}
    title = title.strip()[:200]

    agent = args.get("agent") or "default"
    if not isinstance(agent, str) or not agent.strip():
        return {"ok": False, "error": "agent must be a non-empty string"}
    agent = agent.strip()[:120]

    try:
        run_at_ts = _parse_run_at(args.get("run_at"))
        delay_sec = _positive_int(args.get("delay_seconds"), "delay_seconds", min_value=1, max_value=366 * 24 * 3600)
        interval_sec = _positive_int(args.get("interval_seconds"), "interval_seconds", min_value=60, max_value=366 * 24 * 3600)
        max_runs = _positive_int(args.get("max_runs"), "max_runs", min_value=1, max_value=10000)
        cron_expr = args.get("cron")
        if cron_expr is not None:
            if not isinstance(cron_expr, str) or not cron_expr.strip():
                raise ValueError("cron must be a non-empty string")
            cron_expr = cron_expr.strip()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    now = _now()
    schedule_count = int(run_at_ts is not None) + int(delay_sec is not None) + int(interval_sec is not None) + int(cron_expr is not None)
    if schedule_count == 0:
        return {"ok": False, "error": "provide one of run_at, delay_seconds, interval_seconds, or cron"}
    if cron_expr is not None and schedule_count > 1:
        return {"ok": False, "error": "cron cannot be combined with run_at, delay_seconds, or interval_seconds"}

    try:
        if cron_expr is not None:
            kind = "cron"
            next_run_ts = _next_cron_ts(cron_expr, now)
            max_runs = max_runs
        elif interval_sec is not None:
            kind = "interval"
            next_run_ts = run_at_ts or (now + delay_sec if delay_sec is not None else now + interval_sec)
            max_runs = max_runs
        else:
            kind = "once"
            next_run_ts = run_at_ts or (now + int(delay_sec or 0))
            max_runs = 1
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    min_due = now + _min_delay_sec()
    if next_run_ts < min_due:
        next_run_ts = min_due

    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    task_id = new_id("task")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (
                id, title, prompt, agent, kind, status, run_at_ts, interval_sec, cron_expr,
                next_run_ts, run_count, max_runs, created_ts, updated_ts, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 'enabled', ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                task_id,
                title,
                prompt,
                agent,
                kind,
                run_at_ts,
                interval_sec,
                cron_expr,
                next_run_ts,
                max_runs,
                now,
                now,
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
            ),
        )
        row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    return {"ok": True, "task": _task_row_to_dict(row)}


def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    init_db()
    status = args.get("status")
    if status is not None and not isinstance(status, str):
        return {"ok": False, "error": "status must be a string"}
    try:
        limit = int(args.get("limit") or 50)
    except Exception:
        limit = 50
    limit = max(1, min(limit, 200))

    where = ""
    params: list[Any] = []
    if isinstance(status, str) and status.strip():
        where = "WHERE status=?"
        params.append(status.strip())
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM agent_tasks {where} ORDER BY COALESCE(next_run_ts, updated_ts) ASC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return {"ok": True, "tasks": [_task_row_to_dict(row) for row in rows]}


def get_task(args: dict[str, Any]) -> dict[str, Any]:
    init_db()
    task_id = args.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        return {"ok": False, "error": "id must be a non-empty string"}
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id.strip(),)).fetchone()
    if row is None:
        return {"ok": False, "error": "task not found"}
    return {"ok": True, "task": _task_row_to_dict(row)}


def _task_run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(row["payload_json"] or "{}")
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "due_ts": row["due_ts"],
        "due_at": _iso(row["due_ts"]),
        "started_ts": row["started_ts"],
        "started_at": _iso(row["started_ts"]),
        "finished_ts": row["finished_ts"],
        "finished_at": _iso(row["finished_ts"]),
        "agent_run_id": row["agent_run_id"],
        "ok": None if row["ok"] is None else bool(row["ok"]),
        "output_text": row["output_text"],
        "error": row["error"],
        "payload": payload,
    }


def list_task_runs(args: dict[str, Any]) -> dict[str, Any]:
    init_db()
    task_id = args.get("id") or args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return {"ok": False, "error": "id must be a non-empty string"}
    try:
        limit = int(args.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 100))
    with _connect() as conn:
        task = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id.strip(),)).fetchone()
        if task is None:
            return {"ok": False, "error": "task not found"}
        rows = conn.execute(
            """
            SELECT * FROM agent_task_runs
            WHERE task_id=?
            ORDER BY started_ts DESC
            LIMIT ?
            """,
            (task_id.strip(), limit),
        ).fetchall()
    return {"ok": True, "task": _task_row_to_dict(task), "runs": [_task_run_row_to_dict(row) for row in rows]}


def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
    init_db()
    task_id = args.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        return {"ok": False, "error": "id must be a non-empty string"}
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agent_tasks SET status='cancelled', next_run_ts=NULL, updated_ts=? WHERE id=? AND status NOT IN ('completed','cancelled')",
            (now, task_id.strip()),
        )
        row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id.strip(),)).fetchone()
    if row is None:
        return {"ok": False, "error": "task not found"}
    return {"ok": True, "cancelled": cur.rowcount > 0, "task": _task_row_to_dict(row)}


@dataclass(frozen=True)
class _SyntheticRequest:
    headers: dict[str, str]


def _scheduled_prompt(row: sqlite3.Row, due_ts: int) -> str:
    return (
        "A scheduled Nexus agent task is due.\n\n"
        f"Task id: {row['id']}\n"
        f"Title: {row['title']}\n"
        f"Due at: {_iso(due_ts)}\n"
        f"Run count before this run: {row['run_count']}\n\n"
        "Task prompt:\n"
        f"{row['prompt']}"
    )


def _compute_next(row: sqlite3.Row, now: int) -> int | None:
    kind = row["kind"]
    if kind == "interval":
        interval = int(row["interval_sec"] or 0)
        if interval <= 0:
            return None
        base = int(row["next_run_ts"] or now)
        while base <= now:
            base += interval
        return base
    if kind == "cron":
        expr = row["cron_expr"] or ""
        return _next_cron_ts(expr, now)
    return None


async def _execute_task(row: sqlite3.Row) -> None:
    started = _now()
    task_id = str(row["id"])
    due_ts = int(row["next_run_ts"] or started)
    task_run_id = new_id("taskrun")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO agent_task_runs (id, task_id, due_ts, started_ts) VALUES (?, ?, ?, ?)",
            (task_run_id, task_id, due_ts, started),
        )

    payload: dict[str, Any] = {}
    ok = False
    output_text = ""
    error = ""
    agent_run_id = ""
    try:
        from app.agent_runtime_v1 import run_agent_v1

        timeout = float(getattr(S, "AGENT_TASKS_RUN_TIMEOUT_SEC", 1800) or 1800)
        payload, _backend, _model = await asyncio.wait_for(
            run_agent_v1(
                req=_SyntheticRequest(headers={}),  # type: ignore[arg-type]
                run_req=AgentRunRequest(agent=str(row["agent"] or "default"), input=_scheduled_prompt(row, due_ts)),
            ),
            timeout=timeout,
        )
        ok = bool(payload.get("ok"))
        output_text = str(payload.get("output_text") or "")[:20_000]
        error = str(payload.get("error") or "")[:20_000]
        agent_run_id = str(payload.get("run_id") or "")
    except Exception as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"[:20_000]

    finished = _now()
    next_status = "completed"
    next_run_ts: int | None = None
    run_count = int(row["run_count"] or 0) + 1
    max_runs = row["max_runs"]
    try:
        if row["kind"] in {"interval", "cron"} and (max_runs is None or run_count < int(max_runs)):
            next_run_ts = _compute_next(row, finished)
            if next_run_ts is not None:
                next_status = "enabled"
    except Exception as exc:
        error = (error + f"\nnext schedule error: {type(exc).__name__}: {exc}").strip()[:20_000]
        next_status = "error"

    if row["kind"] == "once" and not ok:
        next_status = "error"

    with _connect() as conn:
        conn.execute(
            """
            UPDATE agent_task_runs
            SET finished_ts=?, agent_run_id=?, ok=?, output_text=?, error=?, payload_json=?
            WHERE id=?
            """,
            (
                finished,
                agent_run_id,
                1 if ok else 0,
                output_text,
                error,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False)[:100_000],
                task_run_id,
            ),
        )
        conn.execute(
            """
            UPDATE agent_tasks
            SET status=?, next_run_ts=?, last_run_ts=?, last_run_id=?, last_ok=?, last_error=?,
                run_count=?, updated_ts=?
            WHERE id=?
            """,
            (
                next_status,
                next_run_ts,
                finished,
                agent_run_id,
                1 if ok else 0,
                error or None,
                run_count,
                finished,
                task_id,
            ),
        )


async def _claim_due(limit: int) -> list[sqlite3.Row]:
    now = _now()
    rows: list[sqlite3.Row] = []
    with _connect() as conn:
        candidates = conn.execute(
            """
            SELECT * FROM agent_tasks
            WHERE status='enabled' AND next_run_ts IS NOT NULL AND next_run_ts <= ?
            ORDER BY next_run_ts ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        for row in candidates:
            cur = conn.execute(
                "UPDATE agent_tasks SET status='running', updated_ts=? WHERE id=? AND status='enabled'",
                (now, row["id"]),
            )
            if cur.rowcount == 1:
                claimed = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (row["id"],)).fetchone()
                if claimed is not None:
                    rows.append(claimed)
    return rows


async def _loop() -> None:
    try:
        init_db()
    except Exception as exc:
        log.warning("agent task scheduler init failed: %s: %s", type(exc).__name__, exc)
        return

    stop = _STOP_EVENT
    while stop is not None and not stop.is_set():
        try:
            limit = max(1, int(getattr(S, "AGENT_TASKS_MAX_DUE_PER_TICK", 3) or 3))
            for row in await _claim_due(limit):
                await _execute_task(row)
        except Exception:
            log.exception("agent task scheduler tick failed")

        try:
            poll = max(1.0, float(getattr(S, "AGENT_TASKS_POLL_INTERVAL_SEC", 5.0) or 5.0))
        except Exception:
            poll = 5.0
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except asyncio.TimeoutError:
            pass


async def start_scheduler() -> None:
    global _TASK_LOOP, _STOP_EVENT
    if not getattr(S, "AGENT_TASKS_ENABLED", True):
        return
    init_db()
    if _TASK_LOOP is not None and not _TASK_LOOP.done():
        return
    _STOP_EVENT = asyncio.Event()
    _TASK_LOOP = asyncio.create_task(_loop())


async def stop_scheduler() -> None:
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
