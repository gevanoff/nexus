from __future__ import annotations

import os
import subprocess
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_routes
from app import coding_agent as ca
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


def test_harness_validation_uses_mission_timeout_without_git_credentials(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="long-validation",
        files={"test_app.py": "pass\n"},
        prompt="Validate the fixture.",
        owner="test",
        mission_overrides=cw.coding_mission_overrides(max_runtime_sec=300),
    )
    captured = {}

    def fake_run_process(argv, **kwargs):
        captured.update({"argv": list(argv), **kwargs})
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", fake_run_process)

    result = cw.run_harness_validation_command(
        task["id"],
        argv=["git", "status", "--short"],
        timeout_sec=250,
    )

    assert result["ok"] is True
    assert captured["timeout_sec"] == 250
    assert captured["timeout_limit_sec"] == 300
    assert captured["use_git_credentials"] is False


def test_harness_validation_preserves_argument_whitespace(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-whitespace",
        files={"test_app.py": "pass\n"},
        prompt="Validate exact argument data.",
        owner="test",
    )
    captured = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = list(argv)
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", fake_run_process)

    cw.run_harness_validation_command(
        task["id"],
        argv=["python3", "-c", "import sys; assert sys.argv[1] == '  '", "  "],
    )

    assert captured["argv"][-1] == "  "


def test_harness_validation_rejects_normal_workspace(monkeypatch):
    monkeypatch.setattr(cw, "load_task", lambda task_id: {"id": task_id, "kind": "workspace"})

    with pytest.raises(HTTPException) as exc_info:
        cw.run_harness_validation_command("code_abcdef123456", argv=["python3", "-m", "unittest"])

    assert exc_info.value.status_code == 403


def test_run_process_timeout_override_is_bounded_but_can_exceed_default(monkeypatch, tmp_path):
    monkeypatch.setattr(cw.S, "CODING_COMMAND_TIMEOUT_SEC", 120)
    observed = []

    def fake_run(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cw.subprocess, "run", fake_run)

    cw._run_process(["python3", "--version"], cwd=tmp_path, timeout_sec=250)
    cw._run_process(
        ["python3", "--version"],
        cwd=tmp_path,
        timeout_sec=250,
        timeout_limit_sec=300,
    )

    assert observed == [120, 250]


def test_harness_git_evidence_requests_one_over_changed_file_limit(monkeypatch):
    monkeypatch.setattr(
        cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "harness_eval"},
    )
    captured = {}

    def fake_snapshot(task, *, change_limit):
        captured.setdefault("calls", []).append((task["id"], change_limit))
        return {"diff": {"ok": True}, "changes": {"ok": True}}

    monkeypatch.setattr(cw, "_harness_neutral_git_snapshot", fake_snapshot)

    assert cw.harness_git_diff("code_abcdef123456") == {"ok": True}
    assert cw.harness_git_changes("code_abcdef123456") == {"ok": True}
    assert captured == {"calls": [("code_abcdef123456", 513)] * 2}


def test_harness_git_evidence_ignores_repository_git_controls(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="neutral-git-evidence",
        files={".gitattributes": "*.py -diff\n", "app.py": "VALUE = 1\n"},
        prompt="Change the value.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.external", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "app.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    changes = cw.harness_git_changes(task["id"])
    diff = cw.harness_git_diff(task["id"])

    assert changes["ok"] is True
    assert {item["path"] for item in changes["files"]} == {"app.py"}
    assert diff["ok"] is True
    assert {item["path"] for item in diff["changes"]["files"]} == {"app.py"}
    assert "+VALUE = 2" in diff["diff"]["stdout"]


def test_harness_limits_do_not_depend_on_normal_coding_file_cap(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "file_max_bytes", lambda: 1)

    task = cw.create_harness_task(
        fixture_id="shared-limit",
        files={"value.txt": "valid fixture content"},
        prompt="Inspect the fixture.",
        owner="test",
    )

    evidence = cw.read_harness_file_evidence(task["id"], path="value.txt")
    assert evidence["encoding"] == "utf-8"
    assert evidence["content"] == "valid fixture content"


def test_harness_git_evidence_preserves_literal_paths_and_rename_targets(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="literal-paths",
        files={"café.txt": "old\n", "old name.txt": "rename me\n"},
        prompt="Change files with unusual names.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "café.txt").write_text("new\n", encoding="utf-8")
    (repo / 'quote"file.txt').write_text("added\n", encoding="utf-8")
    subprocess.run(
        ["git", "mv", "old name.txt", "new name.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    changed = cw.harness_git_changes(task["id"])["files"]
    diff_changed = cw.harness_git_diff(task["id"])["changes"]["files"]

    expected = {"café.txt", 'quote"file.txt', "new name.txt"}
    assert {item["path"] for item in changed} == expected
    assert {item["path"] for item in diff_changed} == expected
    rename = next(item for item in changed if item["kind"] == "renamed")
    assert rename["path"] == "new name.txt"
    assert rename["previous_path"] == "old name.txt"


def test_harness_file_evidence_is_strict_text_or_explicit_binary(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="file-evidence",
        files={"text.txt": "hello\n", " spaced.txt ": "exact\n"},
        prompt="Inspect evidence.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "binary.dat").write_bytes(b"\xff\x00payload")
    (repo / "link.txt").symlink_to("text.txt")

    assert cw.read_harness_file_evidence(task["id"], path="text.txt") == {
        "path": "text.txt",
        "size": 6,
        "mode": "100644",
        "encoding": "utf-8",
        "content": "hello\n",
    }
    assert cw.read_harness_file_evidence(task["id"], path="binary.dat") == {
        "path": "binary.dat",
        "size": 9,
        "encoding": "binary",
        "content": None,
    }
    assert cw.read_harness_file_evidence(task["id"], path=" spaced.txt ")["content"] == "exact\n"
    assert cw.read_harness_file_evidence(task["id"], path="link.txt") == {
        "path": "link.txt",
        "size": 8,
        "encoding": "symlink",
        "content": None,
    }


def test_harness_file_evidence_rejects_normal_workspace(monkeypatch):
    monkeypatch.setattr(
        cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "workspace"},
    )

    with pytest.raises(HTTPException) as exc_info:
        cw.read_harness_file_evidence("code_abcdef123456", path="app.py")

    assert exc_info.value.status_code == 403


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
    abandoned = cw.create_harness_task(
        fixture_id="abandoned",
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
    abandoned_initializing = cw.create_harness_task(
        fixture_id="abandoned-initializing",
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
    raw_abandoned = cw.load_task(abandoned["id"])
    raw_abandoned["harness_expires_at"] = now - 100
    raw_abandoned["updated_at"] = now - 60
    cw.save_task(raw_abandoned)
    raw_initializing = cw.load_task(abandoned_initializing["id"])
    raw_initializing["harness_expires_at"] = now - 100
    raw_initializing["status"] = "initializing"
    raw_initializing["agent_status"] = "idle"
    raw_initializing["updated_at"] = now - 60
    cw.save_task(raw_initializing)

    result = cw.cleanup_expired_harness_tasks(now=now)

    expected_purged = {
        terminal["id"],
        abandoned["id"],
        abandoned_initializing["id"],
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


def test_harness_deletion_refuses_an_active_validation(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-delete-race",
        files={"app.py": "value\n"},
        prompt="Validate safely.",
        owner="test",
    )
    started = threading.Event()
    release = threading.Event()
    failures = []

    def fake_run_process(argv, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    def validate():
        try:
            cw.run_harness_validation_command(task["id"], argv=["python3", "-V"])
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "_run_process", fake_run_process)
    worker = threading.Thread(target=validate)
    worker.start()
    assert started.wait(timeout=5)
    try:
        with pytest.raises(HTTPException) as exc_info:
            cw.delete_harness_task(task["id"])
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert cw.delete_harness_task(task["id"])["ok"] is True


def test_harness_deletion_waits_for_cancelled_agent_tool_worker(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="agent-tool-delete-race",
        files={"app.py": "value\n"},
        prompt="Run a blocking command.",
        owner="test",
    )
    started = threading.Event()
    release = threading.Event()
    failures = []

    def fake_run_task_command(task_id, **kwargs):
        started.set()
        assert release.wait(timeout=30)
        stale = cw.load_task(task_id)
        stale["worker_finished"] = True
        cw.save_task(stale)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    def run_tool():
        try:
            ca._run_tool_worker(
                task["id"],
                "coding_run_command",
                {"argv": ["python3", "-V"]},
                git_token_value=None,
            )
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "run_task_command", fake_run_task_command)
    worker = threading.Thread(target=run_tool)
    worker.start()
    assert started.wait(timeout=30)
    try:
        with pytest.raises(HTTPException) as exc_info:
            cw.delete_harness_task(task["id"])
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert failures == []
    assert cw.load_task(task["id"])["worker_finished"] is True
    assert cw.delete_harness_task(task["id"])["ok"] is True


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
async def test_harness_validation_route_uses_dedicated_runner(monkeypatch):
    captured = {}
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: SimpleNamespace(id=7))

    def fake_validation(task_id, **kwargs):
        captured.update({"task_id": task_id, **kwargs})
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(coding_routes.cw, "run_harness_validation_command", fake_validation)
    response = await coding_routes.v1_coding_harness_validation(
        SimpleNamespace(),
        "code_abcdef123456",
        coding_routes.CodingCommandRequest(
            argv=["python3", "-m", "unittest"],
            timeout_sec=250,
        ),
    )

    assert response == {"result": {"ok": True, "returncode": 0}}
    assert captured == {
        "task_id": "code_abcdef123456",
        "argv": ["python3", "-m", "unittest"],
        "cwd": None,
        "timeout_sec": 250,
    }


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
        "delete_harness_task",
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
    monkeypatch.setattr(
        coding_routes.cw,
        "delete_harness_task",
        lambda task_id: {"ok": True, "task_id": task_id},
    )

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
