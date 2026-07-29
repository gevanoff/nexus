from __future__ import annotations

import pytest

from app import coding_agent_guarded
from app import coding_workspace_reconciliation as reconciliation


def _task(**overrides):
    value = {
        "id": "code_abcdef123456",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "repo_path": "/tmp/repo",
        "base_branch": "main",
        "branch_name": "agent/example",
        "agent_status": "paused",
        "agent_start_head": "base-sha",
        "last_checkpoint_commit": "work-sha",
        "last_pr_output": "https://github.com/gevanoff/nexus/pull/50",
        "agent_events": [],
        "terminal_result": {},
    }
    value.update(overrides)
    return value


def _mutator_store(task):
    def mutate(_task_id, mutator):
        mutator(task)
        return task

    return mutate


def _clean_summary():
    return {"ok": True, "counts": {"total": 0}, "files": []}


def test_merged_pull_request_marks_workspace_integrated(monkeypatch):
    task = _task()
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(reconciliation.cw, "mutate_task", _mutator_store(task))
    monkeypatch.setattr(reconciliation.cw, "git_head", lambda _task_id: {"ok": True, "commit": "work-sha"})
    monkeypatch.setattr(reconciliation.cw, "git_change_summary", lambda _task_id: _clean_summary())
    monkeypatch.setattr(
        reconciliation.cw,
        "_github_api_request",
        lambda *args, **kwargs: {
            "ok": True,
            "body": {
                "number": 50,
                "state": "closed",
                "merged_at": "2026-07-27T23:00:00Z",
                "html_url": "https://github.com/gevanoff/nexus/pull/50",
                "merge_commit_sha": "merged-sha",
                "head": {"sha": "work-sha"},
            },
        },
    )

    result = reconciliation.reconcile_task_before_run("code_abcdef123456", git_token_value="token")

    assert result["proceed"] is False
    assert task["agent_status"] == "completed"
    assert task["agent_stop_reason_code"] == "work_already_integrated"
    assert task["terminal_result"]["stop_reason_code"] == "work_already_integrated"
    assert task["agent_events"][-1]["type"] == "work_already_integrated"


def test_open_pull_request_remains_resumable(monkeypatch):
    task = _task()
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(
        reconciliation.cw,
        "_github_api_request",
        lambda *args, **kwargs: {
            "ok": True,
            "body": {
                "number": 50,
                "state": "open",
                "merged_at": None,
                "html_url": "https://github.com/gevanoff/nexus/pull/50",
                "head": {"sha": "work-sha"},
            },
        },
    )

    result = reconciliation.reconcile_task_before_run("code_abcdef123456", git_token_value="token")

    assert result["proceed"] is True
    assert result["status"] == "open_pull_request"
    assert task["agent_status"] == "paused"


def test_new_workspace_without_durable_run_history_is_not_reconciled(monkeypatch):
    task = _task(
        agent_status="idle",
        agent_start_head="base-sha",
        last_checkpoint_commit="",
        last_pr_output="",
        agent_runs=[],
    )
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)

    result = reconciliation.reconcile_task_before_run("code_abcdef123456")

    assert result["proceed"] is True
    assert result["status"] == "not_applicable"


def test_local_ancestor_marks_workspace_integrated_when_pr_lookup_unknown(monkeypatch):
    task = _task(last_pr_output="", last_pushed_at=123.0)
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(reconciliation.cw, "mutate_task", _mutator_store(task))
    monkeypatch.setattr(reconciliation.cw, "git_head", lambda _task_id: {"ok": True, "commit": "work-sha"})
    monkeypatch.setattr(reconciliation.cw, "git_change_summary", lambda _task_id: _clean_summary())
    monkeypatch.setattr(
        reconciliation,
        "_local_integration_state",
        lambda *args, **kwargs: {
            "known": True,
            "integrated": True,
            "source": "git_ancestry",
            "head": "work-sha",
            "base_ref": "refs/remotes/origin/main",
        },
    )

    result = reconciliation.reconcile_task_before_run("code_abcdef123456")

    assert result["proceed"] is False
    assert task["integration_reconciliation"]["source"] == "git_ancestry"


def test_unknown_reconciliation_does_not_block_resume(monkeypatch):
    task = _task(last_pr_output="", last_pushed_at=123.0)
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(reconciliation.cw, "git_head", lambda _task_id: {"ok": True, "commit": "work-sha"})
    monkeypatch.setattr(reconciliation.cw, "git_change_summary", lambda _task_id: _clean_summary())
    monkeypatch.setattr(
        reconciliation,
        "_local_integration_state",
        lambda *args, **kwargs: {
            "known": False,
            "integrated": False,
            "source": "git_ancestry",
            "error": "network unavailable",
        },
    )

    result = reconciliation.reconcile_task_before_run("code_abcdef123456")

    assert result["proceed"] is True
    assert result["status"] == "reconciliation_unknown"


@pytest.mark.asyncio
async def test_guarded_start_does_not_call_agent_for_integrated_workspace(monkeypatch):
    integrated_task = _task(agent_status="completed")
    called = False

    async def fake_reconcile(*args, **kwargs):
        return {"proceed": False, "task": integrated_task}

    async def fake_start(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(coding_agent_guarded.cw, "load_task", lambda _task_id: integrated_task)
    monkeypatch.setattr(coding_agent_guarded._agent, "_active_runner", lambda _task_id: None)
    monkeypatch.setattr(coding_agent_guarded, "reconcile_before_run", fake_reconcile)
    monkeypatch.setattr(coding_agent_guarded._agent, "start_agent_run", fake_start)
    monkeypatch.setattr(
        coding_agent_guarded.cw,
        "public_task",
        lambda task: {"agent": {"status": task["agent_status"]}},
    )

    result = await coding_agent_guarded.start_agent_run("code_abcdef123456")

    assert called is False
    assert result["agent"]["status"] == "completed"


@pytest.mark.asyncio
async def test_guarded_start_delegates_when_work_is_not_integrated(monkeypatch):
    task = _task(agent_status="paused")

    async def fake_reconcile(*args, **kwargs):
        return {"proceed": True, "task": task}

    async def fake_start(*args, **kwargs):
        return {"agent": {"status": "queued"}}

    monkeypatch.setattr(coding_agent_guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(coding_agent_guarded._agent, "_active_runner", lambda _task_id: None)
    monkeypatch.setattr(coding_agent_guarded, "reconcile_before_run", fake_reconcile)
    monkeypatch.setattr(coding_agent_guarded._agent, "start_agent_run", fake_start)

    result = await coding_agent_guarded.start_agent_run("code_abcdef123456")

    assert result["agent"]["status"] == "queued"


@pytest.mark.asyncio
async def test_guarded_start_delegates_active_run_before_reconciliation(monkeypatch):
    task = _task(agent_status="running")
    reconciled = False
    started = False

    async def fake_reconcile(*args, **kwargs):
        nonlocal reconciled
        reconciled = True
        return {"proceed": False, "task": task}

    async def fake_start(*args, **kwargs):
        nonlocal started
        started = True
        return {"agent": {"status": "running"}}

    monkeypatch.setattr(coding_agent_guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(coding_agent_guarded._agent, "_active_runner", lambda _task_id: object())
    monkeypatch.setattr(coding_agent_guarded, "reconcile_before_run", fake_reconcile)
    monkeypatch.setattr(coding_agent_guarded._agent, "start_agent_run", fake_start)

    result = await coding_agent_guarded.start_agent_run(task["id"])

    assert started is True
    assert reconciled is False
    assert result["agent"]["status"] == "running"


@pytest.mark.asyncio
async def test_guarded_start_recovers_stale_active_state_before_reconciliation(monkeypatch):
    task = _task(agent_status="running")
    recovered_task = _task(agent_status="paused")
    integrated_task = _task(agent_status="completed")
    order = []
    started = False

    def fake_recover(task_id, current):
        assert current is task
        order.append("recover")
        return recovered_task

    async def fake_reconcile(*args, **kwargs):
        order.append("reconcile")
        return {"proceed": False, "task": integrated_task}

    async def fake_start(*args, **kwargs):
        nonlocal started
        started = True
        return {}

    monkeypatch.setattr(coding_agent_guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(coding_agent_guarded._agent, "_active_runner", lambda _task_id: None)
    monkeypatch.setattr(coding_agent_guarded._agent, "_mark_stale_agent_paused", fake_recover)
    monkeypatch.setattr(coding_agent_guarded, "reconcile_before_run", fake_reconcile)
    monkeypatch.setattr(coding_agent_guarded._agent, "start_agent_run", fake_start)
    monkeypatch.setattr(
        coding_agent_guarded.cw,
        "public_task",
        lambda value: {"agent": {"status": value["agent_status"]}},
    )

    result = await coding_agent_guarded.start_agent_run(task["id"])

    assert order == ["recover", "reconcile"]
    assert started is False
    assert result["agent"]["status"] == "completed"
