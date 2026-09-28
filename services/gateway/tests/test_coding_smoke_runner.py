from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app import coding_smoke_runner as runner


def test_run_one_recovers_transient_clone_failure_on_same_task(monkeypatch):
    create_calls = []
    recovered = []
    archived = []

    def create_task(**kwargs):
        create_calls.append(kwargs)
        return {
            "id": "code_clone_failed",
            "status": "error",
            "error": "git clone failed",
            "repo_url": "https://github.com/gevanoff/nexus.git",
        }

    def retry_failed_initialization(_cw, task_id):
        recovered.append(task_id)
        return {
            "id": task_id,
            "status": "ready",
            "repo_url": "https://github.com/gevanoff/nexus.git",
        }

    async def start_agent_run(*_args, **_kwargs):
        return {"agent": {"status": "completed"}}

    monkeypatch.setattr(runner.cw, "create_task", create_task)
    monkeypatch.setattr(
        runner.coding_network_resilience,
        "retry_failed_initialization",
        retry_failed_initialization,
    )
    monkeypatch.setattr(runner.ca, "start_agent_run", start_agent_run)
    monkeypatch.setattr(
        runner.coding_model_policy,
        "describe_workspace_model",
        lambda _model: {"run_policy": "active"},
    )
    monkeypatch.setattr(
        runner.cw,
        "load_task",
        lambda _task_id: {"agent": {"status": "completed"}},
    )
    monkeypatch.setattr(runner.cw, "public_task", lambda task: task)
    monkeypatch.setattr(
        runner.cw,
        "inspect_task",
        lambda _task_id, **_kwargs: {"task": {}},
    )
    monkeypatch.setattr(runner, "_verify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner.cw,
        "archive_task",
        lambda task_id, **_kwargs: archived.append(task_id)
        or {"ok": True, "archive_id": "archive_retry_ok"},
    )
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is True
    assert report["task_id"] == "code_clone_failed"
    assert len(create_calls) == 1
    assert recovered == ["code_clone_failed"]
    phases = {
        phase["name"]: phase for phase in report["phases"]
    }
    assert phases["create"]["ok"] is False
    assert phases["create_recovery"]["ok"] is True
    assert archived == ["code_clone_failed"]


def test_run_one_does_not_recreate_non_transient_clone_failure(monkeypatch):
    create_calls = []
    recovery_calls = []

    def create_task(**kwargs):
        create_calls.append(kwargs)
        return {
            "id": "code_bad_config",
            "status": "error",
            "error": "git clone failed",
        }

    def reject_recovery(_cw, task_id):
        recovery_calls.append(task_id)
        raise HTTPException(status_code=409, detail="non-transient clone failure")

    monkeypatch.setattr(runner.cw, "create_task", create_task)
    monkeypatch.setattr(
        runner.coding_network_resilience,
        "retry_failed_initialization",
        reject_recovery,
    )
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is False
    assert report["task_id"] == "code_bad_config"
    assert report["error"] == "workspace creation failed: git clone failed"
    assert len(create_calls) == 1
    assert recovery_calls == ["code_bad_config"]
    recovery_phase = next(
        phase for phase in report["phases"]
        if phase["name"] == "create_recovery"
    )
    assert recovery_phase["ok"] is False


def test_run_one_aborts_suite_when_timed_out_agent_does_not_settle(monkeypatch):
    wait_timeouts = []
    pause_calls = []

    async def wait_for_agent_terminal(
        _task_id,
        *,
        timeout_sec,
        poll_sec,
    ):
        del poll_sec
        wait_timeouts.append(timeout_sec)
        return (
            False,
            {"agent": {"status": "running"}},
            {"task": {"attention": []}},
        )

    async def start_agent_run(*_args, **_kwargs):
        return {"agent": {"status": "running"}}

    async def request_pause(task_id):
        pause_calls.append(task_id)

    monkeypatch.setattr(
        runner.coding_model_policy,
        "describe_workspace_model",
        lambda _model: {"run_policy": "active"},
    )
    monkeypatch.setattr(
        runner.cw,
        "create_task",
        lambda **_kwargs: {
            "id": "code_never_settles",
            "status": "ready",
            "repo_url": "https://github.com/gevanoff/nexus.git",
        },
    )
    monkeypatch.setattr(runner.ca, "start_agent_run", start_agent_run)
    monkeypatch.setattr(runner, "_wait_for_agent_terminal", wait_for_agent_terminal)
    monkeypatch.setattr(runner.ca, "request_pause", request_pause)
    monkeypatch.setattr(runner.ca, "agent_run_active", lambda _task_id: True)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_TIMEOUT_SEC", 7)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_COMPLETION_GRACE_SEC", 3)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_PAUSE_SETTLE_SEC", 0)
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is False
    assert report["abort_suite"] is True
    assert report["error"] == (
        "coding run timed out after 7s and pause did not settle"
    )
    assert wait_timeouts == [7, 3]
    assert pause_calls == ["code_never_settles"]


def test_run_one_aborts_suite_when_pause_request_fails(monkeypatch):
    async def wait_for_agent_terminal(*_args, **_kwargs):
        return False, {"agent": {"status": "running"}}, {"task": {}}

    async def start_agent_run(*_args, **_kwargs):
        return {"agent": {"status": "running"}}

    async def request_pause(_task_id):
        raise RuntimeError("simulated pause failure")

    monkeypatch.setattr(
        runner.coding_model_policy,
        "describe_workspace_model",
        lambda _model: {"run_policy": "active"},
    )
    monkeypatch.setattr(
        runner.cw,
        "create_task",
        lambda **_kwargs: {"id": "code_pause_failed", "status": "ready"},
    )
    monkeypatch.setattr(runner.ca, "start_agent_run", start_agent_run)
    monkeypatch.setattr(runner, "_wait_for_agent_terminal", wait_for_agent_terminal)
    monkeypatch.setattr(runner.ca, "request_pause", request_pause)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_TIMEOUT_SEC", 7)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_COMPLETION_GRACE_SEC", 0)
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is False
    assert report["abort_suite"] is True
    assert report["error"] == (
        "coding run timed out after 7s and pause request failed (RuntimeError)"
    )


def test_run_one_uses_completion_grace_before_requesting_pause(monkeypatch):
    wait_calls = []
    archived = []

    async def wait_for_agent_terminal(
        _task_id,
        *,
        timeout_sec,
        poll_sec,
    ):
        wait_calls.append((timeout_sec, poll_sec))
        if len(wait_calls) == 1:
            return False, {"agent": {"status": "running"}}, {"task": {}}
        return True, {"agent": {"status": "completed"}}, {"task": {}}

    async def start_agent_run(*_args, **_kwargs):
        return {"agent": {"status": "running"}}

    async def unexpected_pause(_task_id):
        raise AssertionError("completion grace should avoid pause")

    monkeypatch.setattr(
        runner.coding_model_policy,
        "describe_workspace_model",
        lambda _model: {"run_policy": "active"},
    )
    monkeypatch.setattr(
        runner.cw,
        "create_task",
        lambda **_kwargs: {"id": "code_graceful", "status": "ready"},
    )
    monkeypatch.setattr(runner.ca, "start_agent_run", start_agent_run)
    monkeypatch.setattr(runner, "_wait_for_agent_terminal", wait_for_agent_terminal)
    monkeypatch.setattr(runner.ca, "request_pause", unexpected_pause)
    monkeypatch.setattr(runner, "_verify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner.cw,
        "archive_task",
        lambda task_id, **_kwargs: archived.append(task_id)
        or {"ok": True, "archive_id": "archive_graceful"},
    )
    monkeypatch.setattr(runner.S, "CODING_SMOKE_TIMEOUT_SEC", 7)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_COMPLETION_GRACE_SEC", 3)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_POLL_SEC", 10)
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is True
    assert wait_calls == [(7, 10), (3, 2.0)]
    assert archived == ["code_graceful"]


def test_run_one_aborts_suite_when_monitoring_fails_after_start(monkeypatch):
    async def wait_for_agent_terminal(*_args, **_kwargs):
        raise RuntimeError("simulated monitoring failure")

    async def start_agent_run(*_args, **_kwargs):
        return {"agent": {"status": "running"}}

    monkeypatch.setattr(
        runner.coding_model_policy,
        "describe_workspace_model",
        lambda _model: {"run_policy": "active"},
    )
    monkeypatch.setattr(
        runner.cw,
        "create_task",
        lambda **_kwargs: {"id": "code_monitor_failed", "status": "ready"},
    )
    monkeypatch.setattr(runner.ca, "start_agent_run", start_agent_run)
    monkeypatch.setattr(runner, "_wait_for_agent_terminal", wait_for_agent_terminal)
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is False
    assert report["abort_suite"] is True
    assert report["error"] == "RuntimeError: simulated monitoring failure"


def test_start_failure_without_active_runner_does_not_abort_suite(monkeypatch):
    async def start_agent_run(*_args, **_kwargs):
        raise RuntimeError("simulated startup validation failure")

    monkeypatch.setattr(
        runner.coding_model_policy,
        "describe_workspace_model",
        lambda _model: {"run_policy": "active"},
    )
    monkeypatch.setattr(
        runner.cw,
        "create_task",
        lambda **_kwargs: {"id": "code_start_failed", "status": "ready"},
    )
    monkeypatch.setattr(runner.ca, "start_agent_run", start_agent_run)
    monkeypatch.setattr(runner.ca, "agent_run_active", lambda _task_id: False)
    monkeypatch.setattr(runner, "_write_report", lambda _report: None)

    report = asyncio.run(
        runner.run_one(model="coder", profile_id="fixture_median")
    )

    assert report["ok"] is False
    assert "abort_suite" not in report
    assert report["error"] == "RuntimeError: simulated startup validation failure"


def test_run_suite_stops_after_unsettled_runner(monkeypatch):
    calls = []

    async def run_one(*, model, profile_id):
        calls.append((model, profile_id))
        return {"ok": False, "abort_suite": True, "task_id": "code_active"}

    monkeypatch.setattr(runner, "run_one", run_one)
    monkeypatch.setattr(runner.S, "CODING_SMOKE_MODELS", "coder")
    monkeypatch.setattr(
        runner.S,
        "CODING_SMOKE_PROFILES",
        "fixture_median,fixture_inventory",
    )
    monkeypatch.setattr(runner.S, "CODING_SMOKE_WEEKLY_MODELS", "")

    asyncio.run(runner.run_suite())

    assert calls == [("coder", "fixture_median")]
