from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import types

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

import app
from app import sentinel_runtime


def _sentinel_events(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.sqlite"
    monkeypatch.setattr(sentinel_runtime, "_db_path", lambda: str(db_path))
    sentinel_runtime.init_db()
    return db_path


def _agent_tasks_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(sentinel_runtime.S, "AGENT_TASKS_DB_PATH", str(db_path), raising=False)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE agent_tasks (
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
        CREATE TABLE agent_task_runs (
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
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_requested_archive_analysis_queues_immediately_and_deduplicates(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    archive_id = "code_abcdef123456.1700000000.deadbeef"
    archive_item = {
        "archive_id": archive_id,
        "analysis": {"requested_mode": "immediate", "target": "local", "status": "pending", "local_model": "coder"},
    }
    updates = []
    started = asyncio.Event()
    release = asyncio.Event()

    coding_workspace = types.SimpleNamespace(
        update_archived_task_settings=lambda next_archive_id, **kwargs: updates.append((next_archive_id, kwargs)) or archive_item,
        get_archived_task=lambda next_archive_id: archive_item,
        mark_archived_analysis=lambda *args, **kwargs: archive_item,
    )
    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)

    async def _analyze(next_archive_id: str):
        assert next_archive_id == archive_id
        started.set()
        await release.wait()
        return {"ok": True, "summary": "Analysis completed."}

    monkeypatch.setattr(sentinel_runtime, "_run_archived_local_analysis", _analyze)
    sentinel_runtime._ARCHIVE_ANALYSIS_TASKS.clear()

    queued = await sentinel_runtime.request_archived_analysis(archive_id, analysis_model="coder", preserve=True)
    await started.wait()
    task = sentinel_runtime._ARCHIVE_ANALYSIS_TASKS[archive_id]
    duplicate = await sentinel_runtime.request_archived_analysis(archive_id, analysis_model="coder", preserve=True)

    assert queued["queued"] is True
    assert duplicate["already_running"] is True
    assert updates == [
        (
            archive_id,
            {
                "preserve": True,
                "analysis_mode": "immediate",
                "analysis_target": "local",
                "analysis_model": "coder",
            },
        )
    ]

    release.set()
    await task
    assert archive_id not in sentinel_runtime._ARCHIVE_ANALYSIS_TASKS


@pytest.mark.asyncio
async def test_sentinel_archive_analyze_route_uses_dedicated_queue(monkeypatch):
    from app import ui_routes

    calls = []

    async def _request(archive_id: str, *, analysis_model: str | None, preserve: bool | None):
        calls.append((archive_id, analysis_model, preserve))
        return {"ok": True, "queued": True}

    monkeypatch.setattr(ui_routes, "_require_ui_access", lambda req: None)
    monkeypatch.setattr(ui_routes, "_require_admin", lambda req: object())
    monkeypatch.setattr(ui_routes.sentinel_runtime, "request_archived_analysis", _request)

    result = await ui_routes.ui_api_sentinel_archive_analyze(
        object(),
        "code_demo.1700000000.deadbeef",
        ui_routes.SentinelArchiveAnalyzeRequest(analysis_model="coder", preserve=True),
    )

    assert result == {"ok": True, "queued": True}
    assert calls == [("code_demo.1700000000.deadbeef", "coder", True)]


def test_backend_issue_state_waits_for_poll_and_duration_thresholds(monkeypatch):
    monkeypatch.setattr(sentinel_runtime.S, "NEXUS_SENTINEL_BACKEND_ISSUE_MIN_POLLS", 3, raising=False)
    monkeypatch.setattr(sentinel_runtime.S, "NEXUS_SENTINEL_BACKEND_ISSUE_MIN_SEC", 60, raising=False)

    first = sentinel_runtime._backend_issue_state({}, fingerprint="abc", now=1000)
    second = sentinel_runtime._backend_issue_state(first, fingerprint="abc", now=1030)
    third = sentinel_runtime._backend_issue_state(second, fingerprint="abc", now=1060)

    assert first["alert_ready"] is False
    assert second["alert_ready"] is False
    assert third["alert_ready"] is True
    assert third["seen_count"] == 3
    assert third["first_seen_ts"] == 1000


@pytest.mark.asyncio
async def test_sentinel_resumes_idle_waiting_workspace_when_resources_idle(monkeypatch, tmp_path):
    db_path = _sentinel_events(tmp_path, monkeypatch)
    resumed = []
    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "counts": {"total": 1, "attention": 0},
            "tasks": [
                {
                    "id": "code_idle",
                    "status": "ready",
                    "coding_model": "model-b",
                    "needs_attention": False,
                    "attention": [],
                    "safe_actions": [],
                    "agent": {"status": "idle_waiting", "model": "model-b"},
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        resumed.append((task_id, actor))
        return {"agent": {"status": "queued"}}

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    coding_model_policy = types.SimpleNamespace(describe_workspace_model=lambda _: {"run_policy": "immediate"})
    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setitem(sys.modules, "app.coding_model_policy", coding_model_policy)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)
    monkeypatch.setattr(app, "coding_model_policy", coding_model_policy, raising=False)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        state = {}
        summary = await sentinel_runtime._monitor_coding(
            state,
            conn,
            now=1_700_000_000,
            resources_summary={"resource_pressure": 0, "queue_pressure": 0},
        )
        conn.commit()
    finally:
        conn.close()

    assert summary["actions"] == 1
    assert resumed == [("code_idle", "nexus-sentinel-idle")]
    assert any(item["event_type"] == "idle_resume" for item in sentinel_runtime.list_events(limit=10))


@pytest.mark.asyncio
async def test_sentinel_records_failed_coding_attention_without_auto_resume(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    _agent_tasks_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    resumed = []
    sent = []
    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "counts": {"total": 1, "attention": 1},
            "tasks": [
                {
                    "id": "code_123",
                    "status": "ready",
                    "owner": "alice",
                    "owner_user_id": 7,
                    "needs_attention": True,
                    "attention": ["run_failed"],
                    "safe_actions": ["resume", "guide_and_resume"],
                    "recommended_action": "resume",
                    "recent_events": [{"type": "failed", "summary": "backend error"}],
                    "agent": {"status": "failed", "last_event_age_sec": 300},
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        resumed.append((task_id, actor))
        return {"agent": {"status": "queued"}}

    async def _send_message(**kwargs: object):
        sent.append(dict(kwargs))
        return {"ok": True}

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    telegram_notifications = types.SimpleNamespace(
        resolve_notification_target=lambda **_: {
            "enabled": True,
            "chat_id": "123",
            "mention_username": "alice_tg",
            "notify_on_attention": True,
            "notify_on_recovery": True,
        },
        render_coding_workspace_notification=lambda **kwargs: f"note:{kwargs.get('event_kind')}:{kwargs.get('item', {}).get('id')}",
        send_message=_send_message,
    )

    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setitem(sys.modules, "app.telegram_notifications", telegram_notifications)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)
    monkeypatch.setattr(app, "telegram_notifications", telegram_notifications, raising=False)

    result = await sentinel_runtime.run_monitor_once()

    assert result["summary"]["coding"]["attention"] == 1
    assert result["summary"]["coding"]["actions"] == 0
    assert result["summary"]["coding"]["notifications"] == 1
    assert resumed == []
    assert len(sent) == 1
    events = sentinel_runtime.list_events(limit=10)
    kinds = {(item["category"], item["event_type"]) for item in events}
    assert ("coding", "needs_attention") in kinds
    assert ("coding", "auto_resume") not in kinds


@pytest.mark.asyncio
async def test_sentinel_records_scheduled_task_failures(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    tasks_db = _agent_tasks_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    conn = sqlite3.connect(str(tasks_db))
    conn.execute(
        """
        INSERT INTO agent_tasks (
            id, title, prompt, agent, kind, status, next_run_ts, last_run_ts, last_run_id, last_ok, last_error,
            run_count, max_runs, created_ts, updated_ts, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1",
            "Failure task",
            "run",
            "default",
            "once",
            "error",
            None,
            1_700_000_000,
            "run-1",
            0,
            "backend overloaded",
            1,
            1,
            1_700_000_000,
            1_700_000_000,
            "{}",
        ),
    )
    conn.commit()

    await sentinel_runtime.run_monitor_once()

    conn.execute(
        """
        UPDATE agent_tasks
        SET last_run_id=?, last_ok=?, last_error=?, run_count=?, updated_ts=?
        WHERE id=?
        """,
        ("run-2", 0, "backend overloaded", 2, 1_700_000_000, "task-1"),
    )
    conn.commit()
    conn.close()

    await sentinel_runtime.run_monitor_once()
    events = sentinel_runtime.list_events(limit=20, category="scheduled_tasks")
    assert any(item["event_type"] == "failed" and item["subject_id"] == "task-1" for item in events)


@pytest.mark.asyncio
async def test_sentinel_does_not_auto_resume_paused_workspaces(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    _agent_tasks_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    resumed = []
    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "counts": {"total": 1, "attention": 1},
            "tasks": [
                {
                    "id": "code_paused",
                    "status": "ready",
                    "owner": "alice",
                    "owner_user_id": 7,
                    "needs_attention": True,
                    "attention": ["run_paused"],
                    "safe_actions": ["resume", "guide_and_resume"],
                    "recommended_action": "resume",
                    "recent_events": [{"type": "paused", "summary": "paused"}],
                    "agent": {"status": "paused", "last_event_age_sec": 300},
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        resumed.append((task_id, actor))
        return {"agent": {"status": "queued"}}

    async def _send_message(**_: object):
        return {"ok": True}

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    telegram_notifications = types.SimpleNamespace(
        resolve_notification_target=lambda **_: {"enabled": False, "reason": "test"},
        render_coding_workspace_notification=lambda **_: "",
        send_message=_send_message,
    )

    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setitem(sys.modules, "app.telegram_notifications", telegram_notifications)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)
    monkeypatch.setattr(app, "telegram_notifications", telegram_notifications, raising=False)

    result = await sentinel_runtime.run_monitor_once()

    assert result["summary"]["coding"]["attention"] == 1
    assert result["summary"]["coding"]["actions"] == 0
    assert resumed == []


@pytest.mark.asyncio
async def test_sentinel_does_not_auto_resume_repeated_no_tool_call_failures(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    _agent_tasks_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    resumed = []
    coding_workspace = types.SimpleNamespace(
        monitor_tasks=lambda **_: {
            "counts": {"total": 1, "attention": 1},
            "tasks": [
                {
                    "id": "code_loop",
                    "status": "ready",
                    "owner": "alice",
                    "owner_user_id": 7,
                    "needs_attention": True,
                    "attention": ["run_failed", "repeated_no_tool_call"],
                    "safe_actions": ["resume", "guide_and_resume"],
                    "recommended_action": "guide_and_resume",
                    "recent_events": [{"type": "no_tool_call_limit", "summary": "too many prose-only cycles"}],
                    "agent": {"status": "failed", "last_event_age_sec": 300},
                }
            ],
        }
    )

    async def _start_agent_run(task_id: str, actor: str | None = None, **_: object):
        resumed.append((task_id, actor))
        return {"agent": {"status": "queued"}}

    async def _send_message(**_: object):
        return {"ok": True}

    coding_agent = types.SimpleNamespace(start_agent_run=_start_agent_run)
    telegram_notifications = types.SimpleNamespace(
        resolve_notification_target=lambda **_: {"enabled": False, "reason": "test"},
        render_coding_workspace_notification=lambda **_: "",
        send_message=_send_message,
    )

    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setitem(sys.modules, "app.coding_agent", coding_agent)
    monkeypatch.setitem(sys.modules, "app.telegram_notifications", telegram_notifications)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(app, "coding_agent", coding_agent, raising=False)
    monkeypatch.setattr(app, "telegram_notifications", telegram_notifications, raising=False)

    result = await sentinel_runtime.run_monitor_once()

    assert result["summary"]["coding"]["attention"] == 1
    assert result["summary"]["coding"]["actions"] == 0
    assert resumed == []


@pytest.mark.asyncio
async def test_sentinel_prunes_only_unused_images_for_allowlisted_disk_pressure(
    monkeypatch,
    tmp_path,
):
    _sentinel_events(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sentinel_runtime.S,
        "NEXUS_SENTINEL_DOCKER_IMAGE_PRUNE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        sentinel_runtime.S,
        "NEXUS_SENTINEL_DOCKER_IMAGE_PRUNE_HOSTS",
        "stackrot,ada2",
        raising=False,
    )
    monkeypatch.setattr(
        sentinel_runtime.S,
        "NEXUS_SENTINEL_DOCKER_IMAGE_PRUNE_COOLDOWN_SEC",
        86_400,
        raising=False,
    )

    async def _resource_payload():
        return {
            "backends": [],
            "hosts": [
                {
                    "name": "stackrot",
                    "memory": {"total_mb": 1000, "available_mb": 800},
                    "gpus": [],
                    "filesystems": [
                        {
                            "mount": "/",
                            "filesystem": "/dev/nvme0n1p2",
                            "total_mb": 100_000,
                            "used_mb": 95_000,
                            "available_mb": 5_000,
                            "used_pct": 95,
                        }
                    ],
                }
            ],
        }

    calls = []

    async def _call_lifecycle(method, path, *, json_body=None, timeout=None):
        calls.append((method, path, json_body, timeout))
        return {
            "ok": True,
            "decision": "unused_images_pruned",
            "host": "stackrot",
            "reclaimed": "12.5GB",
        }

    monkeypatch.setattr(sentinel_runtime, "_build_resource_payload", _resource_payload)
    monkeypatch.setattr(sentinel_runtime, "call_lifecycle_manager", _call_lifecycle)
    monkeypatch.setattr(
        sentinel_runtime,
        "get_admission_controller",
        lambda: types.SimpleNamespace(get_stats=lambda: {}),
    )

    state = {}
    with sentinel_runtime._connect() as conn:
        first = await sentinel_runtime._monitor_resources(
            state,
            conn,
            now=1_700_000_000,
        )
        second = await sentinel_runtime._monitor_resources(
            state,
            conn,
            now=1_700_000_100,
        )

    assert first["docker_image_prunes"] == 1
    assert second["docker_image_prunes"] == 0
    assert calls == [
        (
            "POST",
            "/v1/lifecycle/hosts/docker/image-prune",
            {"host": "stackrot", "confirmed": True},
            620.0,
        )
    ]
    events = sentinel_runtime.list_events(limit=10, category="resources")
    assert any(item["event_type"] == "docker_image_prune" for item in events)


@pytest.mark.asyncio
async def test_sentinel_analyzes_pending_archived_workspace_and_records_purge(monkeypatch, tmp_path):
    _sentinel_events(tmp_path, monkeypatch)
    monkeypatch.setattr(sentinel_runtime, "_now", lambda: 1_700_000_000)

    archive_item = {
        "archive_id": "code_abcdef123456.1700000000.deadbeef",
        "task_id": "code_abcdef123456",
        "prompt": "Scheduled tasks in the Scheduled Tasks UI have an Edit button that does nothing. Fix it so it works!",
        "paths": {"workspace": "/tmp/archive-workspace"},
        "analysis": {"requested_mode": "immediate", "target": "local", "status": "pending", "local_model": "coder"},
        "retention": {"preserve": False, "delete_after_ts": 1_800_000_000},
    }
    appended = []

    coding_workspace = types.SimpleNamespace(
        cleanup_archived_tasks=lambda **_: {
            "count": 1,
            "purged": [
                {
                    "archive_id": "code_old.1700000000.deadbeef",
                    "task_id": "code_old",
                    "workspace_path": "/tmp/old-workspace",
                    "removed": ["/tmp/old-workspace"],
                }
            ],
        },
        list_archived_tasks=lambda **_: [archive_item],
        get_archived_task=lambda archive_id: archive_item,
        mark_archived_analysis=lambda archive_id, **kwargs: archive_item,
        inspect_archived_task=lambda archive_id, max_diff_chars=12000: {
            "archive": archive_item,
            "task": {
                "prompt": archive_item["prompt"],
                "agent_events": [{"type": "failed", "summary": "workspace claimed success with placeholder changes"}],
                "commands": [{"argv": ["npm", "test"], "ok": True}],
            },
            "diff": {"stat": " services/gateway/package.json | 5 +++++", "diff": "+{\"scripts\":{\"test\":\"vitest\"}}\n+function editSelectedTask() { /* Add logic to edit task */ }"},
            "heuristics": [{"summary": "The archived diff added placeholder or stub text instead of a concrete implementation.", "evidence": ["Add logic to edit task"]}],
        },
        append_archived_finding=lambda archive_id, entry: appended.append((archive_id, entry)) or archive_item,
    )

    monkeypatch.setitem(sys.modules, "app.coding_workspace", coding_workspace)
    monkeypatch.setattr(app, "coding_workspace", coding_workspace, raising=False)
    monkeypatch.setattr(
        sentinel_runtime,
        "_call_archive_analysis_model",
        lambda **_: pytest.MonkeyPatch().context,
    )

    async def _fake_call_archive_analysis_model(**_: object):
        return ("The workspace fabricated a package manifest and left placeholder logic in tasks.js.", "local_vllm", "unsloth/Qwen3-30B-A3B-FP8")

    monkeypatch.setattr(sentinel_runtime, "_call_archive_analysis_model", _fake_call_archive_analysis_model)

    with sentinel_runtime._connect() as conn:
        summary = await sentinel_runtime._monitor_archived_workspaces({}, conn, now=1_700_000_000, resources_summary={"resource_pressure": 0, "queue_pressure": 0})

    assert summary["analyzed"] == 1
    assert summary["purged"] == 1
    assert appended and appended[0][0] == archive_item["archive_id"]
    events = sentinel_runtime.list_events(limit=20, category="archives")
    kinds = {(item["category"], item["event_type"]) for item in events}
    assert ("archives", "purged") in kinds
    assert ("archives", "analysis_completed") in kinds
