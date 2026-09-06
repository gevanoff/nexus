from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_routes
from app import coding_workspace as cw


def _configure_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")


def test_create_harness_task_materializes_local_baseline(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)

    task = cw.create_harness_task(
        fixture_id="small-fix",
        files={"src/app.py": "VALUE = 'broken'\n", "test_app.py": "pass\n"},
        prompt="Fix VALUE.",
        owner="test",
        coding_model="coder",
    )

    assert task["kind"] == "harness_eval"
    assert task["status"] == "ready"
    assert task["repo_url"] == "harness-fixture://small-fix"
    assert task["branch_name"].startswith("nexus-coding-harness/")
    raw = cw.load_task(task["id"])
    assert raw["harness_fixture"] == {
        "id": "small-fix",
        "file_count": 2,
        "total_bytes": len("VALUE = 'broken'\npass\n"),
    }
    assert raw["harness_baseline_commit"]
    assert raw["harness_expires_at"] > raw["created_at"]
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 'broken'\n"
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip() == task["branch_name"]
    assert subprocess.run(
        ["git", "remote"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout == ""
    assert cw.git_diff(task["id"])["changes"]["counts"]["total"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/absolute.py",
        "nested\\windows.py",
        ".git/config",
        ".GIT/config",
        "a//b.py",
    ],
)
def test_create_harness_task_rejects_unsafe_paths_before_writing(monkeypatch, tmp_path, path):
    _configure_roots(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        cw.create_harness_task(
            fixture_id="unsafe",
            files={path: "content"},
            prompt="Do work.",
            owner="test",
        )

    assert exc_info.value.status_code == 400
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "tasks").exists()


def test_create_harness_task_enforces_aggregate_limit(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "_HARNESS_MAX_TOTAL_BYTES", 3)
    monkeypatch.setattr(cw, "file_max_bytes", lambda: 100)

    with pytest.raises(HTTPException) as exc_info:
        cw.create_harness_task(
            fixture_id="too-large",
            files={"a.txt": "aa", "b.txt": "bb"},
            prompt="Do work.",
            owner="test",
        )

    assert exc_info.value.status_code == 413
    assert "aggregate" in str(exc_info.value.detail)


def test_cleanup_expired_harness_tasks_only_removes_settled_terminal_evals(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    terminal = cw.create_harness_task(
        fixture_id="terminal",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    active = cw.create_harness_task(
        fixture_id="active",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    initialization_error = cw.create_harness_task(
        fixture_id="initialization-error",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    finalization_failures = [
        (
            cw.create_harness_task(
                fixture_id=status,
                files={"app.py": "value\n"},
                prompt="Change value.",
                owner="test",
            ),
            status,
        )
        for status in ("failed_finalization", "failed_publish")
    ]
    now = 2_000_000_000.0
    statuses = [
        (terminal["id"], "completed"),
        (active["id"], "running"),
        *((task["id"], status) for task, status in finalization_failures),
    ]
    for task_id, status in statuses:
        raw = cw.load_task(task_id)
        raw["harness_expires_at"] = now - 100
        raw["agent_status"] = status
        raw["agent_finished_at"] = now - 60
        cw.save_task(raw)
    raw_error = cw.load_task(initialization_error["id"])
    raw_error["harness_expires_at"] = now - 100
    raw_error["status"] = "error"
    raw_error["updated_at"] = now - 60
    cw.save_task(raw_error)

    result = cw.cleanup_expired_harness_tasks(now=now)

    expected_purged = {
        terminal["id"],
        initialization_error["id"],
        *(task["id"] for task, _ in finalization_failures),
    }
    assert result["ok"] is True
    assert set(result["purged"]) == expected_purged
    assert result["failures"] == {}
    for task_id in expected_purged:
        with pytest.raises(HTTPException) as missing:
            cw.load_task(task_id)
        assert missing.value.status_code == 404
    assert cw.load_task(active["id"])["agent_status"] == "running"


@pytest.mark.asyncio
async def test_harness_route_uses_durable_agent_runner(monkeypatch):
    user = SimpleNamespace(id=7, username="tester")
    captured = {}
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: user)
    monkeypatch.setattr(coding_routes, "_coding_model_for_user", lambda user, requested=None: requested or "coder")

    def fake_create(**kwargs):
        captured["create"] = kwargs
        return {"id": "code_abcdef123456", "kind": "harness_eval", "status": "ready"}

    async def fake_start(task_id, **kwargs):
        captured["start"] = {"task_id": task_id, **kwargs}
        return {
            "id": task_id,
            "kind": "harness_eval",
            "status": "ready",
            "agent": {"status": "queued"},
        }

    monkeypatch.setattr(coding_routes.cw, "create_harness_task", fake_create)
    monkeypatch.setattr(coding_routes.ca, "start_agent_run", fake_start)

    body = coding_routes.CodingHarnessRunRequest(
        fixture_id="small-fix",
        files={"app.py": "broken\n"},
        prompt="Fix app.py.",
        coding_model="coder",
        max_cycles=8,
        max_runtime_sec=120,
    )
    response = await coding_routes.v1_coding_harness_create_and_run(SimpleNamespace(), body)

    assert response["task"]["agent"]["status"] == "queued"
    assert captured["create"]["fixture_id"] == "small-fix"
    assert captured["create"]["files"] == {"app.py": "broken\n"}
    assert captured["start"]["task_id"] == "code_abcdef123456"
    assert captured["start"]["auto_commit"] is True


@pytest.mark.asyncio
async def test_harness_route_deletes_workspace_when_agent_start_fails(monkeypatch):
    deleted = []
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(
        coding_routes.cw,
        "create_harness_task",
        lambda **kwargs: {"id": "code_abcdef123456", "kind": "harness_eval", "status": "ready"},
    )

    async def fail_start(task_id, **kwargs):
        raise RuntimeError("could not queue")

    monkeypatch.setattr(coding_routes.ca, "start_agent_run", fail_start)
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(
        coding_routes.cw,
        "delete_task",
        lambda task_id: deleted.append(task_id) or {"ok": True},
    )

    body = coding_routes.CodingHarnessRunRequest(
        fixture_id="small-fix",
        files={"app.py": "broken\n"},
        prompt="Fix app.py.",
    )
    with pytest.raises(RuntimeError, match="could not queue"):
        await coding_routes.v1_coding_harness_create_and_run(SimpleNamespace(), body)

    assert deleted == ["code_abcdef123456"]


@pytest.mark.asyncio
async def test_harness_delete_route_is_scoped_to_terminal_harness_tasks(monkeypatch):
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(
        coding_routes.cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "harness_eval", "agent_status": "completed"},
    )
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(coding_routes.cw, "delete_task", lambda task_id: {"ok": True, "task_id": task_id})

    response = await coding_routes.v1_coding_harness_delete_task(
        SimpleNamespace(), "code_abcdef123456"
    )

    assert response["result"] == {"ok": True, "task_id": "code_abcdef123456"}


@pytest.mark.asyncio
async def test_harness_delete_route_rejects_active_or_normal_tasks(monkeypatch):
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(
        coding_routes.cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "workspace", "agent_status": "completed"},
    )
    with pytest.raises(HTTPException) as normal_exc:
        await coding_routes.v1_coding_harness_delete_task(SimpleNamespace(), "code_abcdef123456")
    assert normal_exc.value.status_code == 403

    monkeypatch.setattr(
        coding_routes.cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "harness_eval", "agent_status": "running"},
    )
    with pytest.raises(HTTPException) as active_exc:
        await coding_routes.v1_coding_harness_delete_task(SimpleNamespace(), "code_abcdef123456")
    assert active_exc.value.status_code == 409
