from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_network_resilience as nr
from app import coding_workspace as cw


def _result(*, ok: bool, stderr: str = "", stdout: str = "", status=None):
    value = {
        "ok": ok,
        "returncode": 0 if ok else 128,
        "stderr": stderr,
        "stdout": stdout,
        "duration_ms": 1,
    }
    if status is not None:
        value["status"] = status
    return value


def test_run_process_inherits_explicit_directory_fd(tmp_path):
    directory_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        result = cw._run_process(
            [
                sys.executable,
                "-c",
                "import os, sys; os.fstat(int(sys.argv[1]))",
                str(directory_fd),
            ],
            cwd=tmp_path,
            pass_fds=(directory_fd,),
        )
    finally:
        os.close(directory_fd)

    assert result["ok"] is True


def test_clone_transient_failure_preserves_partial_destination(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "code_123456abcdef"
    destination = workspace / "repo"
    workspace.mkdir(parents=True)
    calls = []

    def original(argv, *, cwd, **kwargs):
        calls.append(list(argv))
        destination.mkdir(parents=True)
        (destination / ".git").mkdir()
        (destination / "partial").write_text("partial", encoding="utf-8")
        return _result(
            ok=False,
            stderr="fatal: unable to access repository: Could not resolve host: github.com",
        )

    result = nr.run_process_with_retry(
        original,
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "main",
            "https://github.com/example/repo.git",
            str(destination),
        ],
        cwd=workspace,
        workspace_root=workspace_root,
        sleep_fn=lambda _: None,
        attempts=4,
        base_delay_sec=0,
    )

    assert result["ok"] is False
    assert len(calls) == 1
    assert result["network_retry_count"] == 0
    assert result["network_retry_recovered"] is False
    assert result["network_retry_history"][0]["kind"] == "dns"
    assert destination.joinpath("partial").read_text(encoding="utf-8") == "partial"


def test_clone_does_not_retry_authentication_failure(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "code_123456abcdef"
    destination = workspace / "repo"
    workspace.mkdir(parents=True)
    calls = 0

    def original(argv, *, cwd, **kwargs):
        nonlocal calls
        calls += 1
        return _result(ok=False, stderr="fatal: Authentication failed for 'https://github.com/example/repo.git/'")

    result = nr.run_process_with_retry(
        original,
        ["git", "clone", "https://github.com/example/repo.git", str(destination)],
        cwd=workspace,
        workspace_root=workspace_root,
        sleep_fn=lambda _: None,
        attempts=4,
        base_delay_sec=0,
    )

    assert result["ok"] is False
    assert calls == 1
    assert result["network_retry_count"] == 0


def test_push_retries_transient_connect_failure(tmp_path):
    calls = 0

    def original(argv, *, cwd, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _result(ok=False, stderr="fatal: unable to access repository: Failed to connect to github.com port 443")
        return _result(ok=True)

    result = nr.run_process_with_retry(
        original,
        ["git", "push", "-u", "origin", "feature/test"],
        cwd=tmp_path,
        workspace_root=tmp_path,
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )

    assert result["ok"] is True
    assert calls == 2
    assert result["network_retry_history"][0]["kind"] == "connect"


def test_local_git_failure_is_not_retried(tmp_path):
    calls = 0

    def original(argv, *, cwd, **kwargs):
        nonlocal calls
        calls += 1
        return _result(ok=False, stderr="fatal: ambiguous argument 'missing-ref'")

    result = nr.run_process_with_retry(
        original,
        ["git", "rev-parse", "missing-ref"],
        cwd=tmp_path,
        workspace_root=tmp_path,
        sleep_fn=lambda _: None,
        attempts=4,
        base_delay_sec=0,
    )

    assert result["ok"] is False
    assert calls == 1


def test_github_get_retries_503_but_post_does_not():
    get_calls = 0

    def get_original(method, path, **kwargs):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            return {"ok": False, "status": 503, "body": {"message": "unavailable"}}
        return {"ok": True, "status": 200, "body": {"ok": True}}

    get_result = nr.github_api_with_retry(
        get_original,
        "GET",
        "/repos/example/repo",
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )
    assert get_result["ok"] is True
    assert get_calls == 2
    assert get_result["network_retry_recovered"] is True

    post_calls = 0

    def post_original(method, path, **kwargs):
        nonlocal post_calls
        post_calls += 1
        return {"ok": False, "status": 503, "body": {"message": "unavailable"}}

    post_result = nr.github_api_with_retry(
        post_original,
        "POST",
        "/user/repos",
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )
    assert post_result["ok"] is False
    assert post_calls == 1


def test_github_post_retries_only_preconnect_dns_failure():
    calls = 0

    def original(method, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": False, "error": "URLError: Temporary failure in name resolution"}
        return {"ok": True, "status": 201, "body": {"id": 1}}

    result = nr.github_api_with_retry(
        original,
        "POST",
        "/user/repos",
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )

    assert result["ok"] is True
    assert calls == 2
    assert result["network_retry_history"][0]["kind"] == "dns"


def test_pr_creation_retries_dns_but_not_ambiguous_server_failure():
    dns_calls = 0

    def dns_original(**kwargs):
        nonlocal dns_calls
        dns_calls += 1
        if dns_calls == 1:
            return {"ok": False, "error": "URLError: Could not resolve host: api.github.com"}
        return {"ok": True, "status": 201, "url": "https://github.com/example/repo/pull/1"}

    dns_result = nr.github_pr_create_with_dns_retry(
        dns_original,
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )
    assert dns_result["ok"] is True
    assert dns_calls == 2

    server_calls = 0

    def server_original(**kwargs):
        nonlocal server_calls
        server_calls += 1
        return {"ok": False, "status": 503, "error": "GitHub API PR creation failed"}

    server_result = nr.github_pr_create_with_dns_retry(
        server_original,
        sleep_fn=lambda _: None,
        attempts=3,
        base_delay_sec=0,
    )
    assert server_result["ok"] is False
    assert server_calls == 1


def test_failed_clone_workspace_can_be_reinitialized(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = workspace_root / "code_123456abcdef"
    repo = workspace / "repo"
    task = {
        "id": "code_123456abcdef",
        "status": "error",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_123456abcdef",
        "workspace_path": str(workspace),
        "repo_path": str(repo),
        "commands": [
            {
                "label": "clone",
                "ok": False,
                "stderr_tail": "fatal: unable to access repository: Could not resolve host: github.com",
                "stdout_tail": "",
            }
        ],
    }
    saved = []
    calls = []

    def run_process(argv, *, cwd, **kwargs):
        calls.append(list(argv))
        if argv[1] == "clone":
            repo.mkdir(parents=True)
            (repo / ".git").mkdir()
        return {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "network_retry_attempts": 1,
        }

    def append_command(current, result, *, label):
        current.setdefault("commands", []).append(
            {
                "label": label,
                "ok": bool(result.get("ok")),
                "stderr_tail": str(result.get("stderr") or ""),
                "stdout_tail": str(result.get("stdout") or ""),
            }
        )

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        save_task=lambda current: saved.append(dict(current)) or current,
        workspace_root=lambda: workspace_root,
        task_workspace_lock=cw.task_workspace_lock,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=append_command,
    )

    recovered = nr.retry_failed_initialization(
        fake_cw,
        task["id"],
        git_token_value="token",
    )

    assert recovered["status"] == "ready"
    assert "error" not in recovered
    assert recovered["initialization_recovery"]["recovered"] is True
    assert calls[0][0:2] == ["git", "clone"]
    assert calls[1][0:3] == ["git", "switch", "-c"]
    assert saved


def test_failed_initialization_retries_transient_clone_with_fresh_destination(
    tmp_path, monkeypatch
):
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = workspace_root / "code_123456abcdef"
    repo = workspace / "repo"
    task = {
        "id": "code_123456abcdef",
        "status": "error",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": "feature/test",
        "workspace_path": str(workspace),
        "repo_path": str(repo),
        "commands": [
            {
                "label": "clone",
                "ok": False,
                "stderr_tail": "fatal: Could not resolve host: github.com",
                "stdout_tail": "",
            }
        ],
    }
    clone_inodes = []
    clone_calls = 0
    commands = []

    def run_process(argv, *, cwd, **kwargs):
        nonlocal clone_calls
        if argv[1] == "clone":
            clone_calls += 1
            repo.mkdir(parents=True)
            clone_inodes.append(repo.stat().st_ino)
            if clone_calls == 1:
                (repo / "partial").write_text("first attempt", encoding="utf-8")
                return _result(
                    ok=False,
                    stderr="fatal: Could not resolve host: github.com token",
                )
            (repo / ".git").mkdir()
        return _result(ok=True)

    def append_command(current, result, *, label):
        commands.append((label, dict(result)))
        current.setdefault("commands", []).append(
            {"label": label, "ok": bool(result.get("ok"))}
        )

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        save_task=lambda current: current,
        workspace_root=lambda: workspace_root,
        task_workspace_lock=cw.task_workspace_lock,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=append_command,
    )
    monkeypatch.setenv("CODING_GIT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("CODING_GIT_RETRY_BASE_SEC", "0")

    recovered = nr.retry_failed_initialization(
        fake_cw, task["id"], git_token_value="token"
    )

    assert recovered["status"] == "ready"
    assert clone_calls == 2
    assert clone_inodes[0] != clone_inodes[1]
    quarantines = list(workspace.glob("repo.partial-*/repo"))
    assert len(quarantines) == 1
    assert quarantines[0].joinpath("partial").read_text(encoding="utf-8") == "first attempt"
    recovery = recovered["initialization_recovery"]
    assert recovery["network_retry_attempts"] == 2
    assert recovery["partial_repo_quarantines"] == [
        f"{quarantines[0].parent.name}/repo"
    ]
    clone_results = [result for label, result in commands if label == "clone-retry"]
    assert len(clone_results) == 2
    assert clone_results[-1]["network_retry_recovered"] is True
    assert "token" not in str(clone_results)


def test_failed_initialization_does_not_retry_nontransient_clone(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = workspace_root / "code_123456abcdef"
    repo = workspace / "repo"
    task = {
        "id": "code_123456abcdef",
        "status": "error",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": "feature/test",
        "workspace_path": str(workspace),
        "repo_path": str(repo),
        "commands": [
            {
                "label": "clone",
                "ok": False,
                "stderr_tail": "fatal: Could not resolve host: github.com",
                "stdout_tail": "",
            }
        ],
    }
    clone_calls = 0

    def run_process(argv, *, cwd, **kwargs):
        nonlocal clone_calls
        if argv[1] == "clone":
            clone_calls += 1
            repo.mkdir(parents=True)
            (repo / "partial").write_text("preserved", encoding="utf-8")
            return _result(ok=False, stderr="fatal: Authentication failed")
        raise AssertionError(f"unexpected command: {argv}")

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        save_task=lambda current: current,
        workspace_root=lambda: workspace_root,
        task_workspace_lock=cw.task_workspace_lock,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=lambda current, result, *, label: current.setdefault(
            "commands", []
        ).append({"label": label, "ok": bool(result.get("ok"))}),
    )
    monkeypatch.setenv("CODING_GIT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("CODING_GIT_RETRY_BASE_SEC", "0")

    with pytest.raises(HTTPException) as excinfo:
        nr.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 503
    assert clone_calls == 1
    quarantines = list(workspace.glob("repo.partial-*/repo"))
    assert len(quarantines) == 1
    assert quarantines[0].joinpath("partial").read_text(encoding="utf-8") == "preserved"
    assert task["initialization_recovery"]["network_retry_attempts"] == 1


def test_failed_initialization_audits_clone_before_quarantine_refusal(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = workspace_root / "code_123456abcdef"
    workspace.mkdir()
    repo = workspace / "repo"
    for suffix in ("one", "two"):
        (workspace / f"repo.partial-{suffix}").mkdir()
    task = {
        "id": "code_123456abcdef",
        "status": "error",
        "error": "original clone failed",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": "main",
        "workspace_path": str(workspace),
        "repo_path": str(repo),
        "commands": [
            {
                "label": "clone",
                "ok": False,
                "stderr_tail": "fatal: Could not resolve host: github.com",
                "stdout_tail": "",
            }
        ],
    }
    saved = []

    def run_process(argv, *, cwd, **_kwargs):
        assert argv[1] == "clone"
        repo.mkdir()
        (repo / "partial").write_text("preserve", encoding="utf-8")
        return _result(
            ok=False,
            stderr="fatal: Could not resolve host: github.com secret-token",
        )

    def append_command(current, result, *, label):
        current.setdefault("commands", []).append(
            {
                "label": label,
                "ok": bool(result.get("ok")),
                "stderr_tail": str(result.get("stderr") or ""),
            }
        )

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        save_task=lambda current: saved.append(dict(current)) or current,
        workspace_root=lambda: workspace_root,
        task_workspace_lock=cw.task_workspace_lock,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=append_command,
    )
    monkeypatch.setenv("CODING_GIT_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("CODING_GIT_RETRY_BASE_SEC", "0")

    with pytest.raises(HTTPException) as excinfo:
        nr.retry_failed_initialization(
            fake_cw,
            task["id"],
            git_token_value="secret-token",
        )

    assert excinfo.value.status_code == 409
    assert "preserved partial repository limit" in str(excinfo.value.detail)
    assert task["status"] == "error"
    assert task["error"] == "git clone failed after initialization retry"
    assert task["initialization_recovery"]["network_retry_attempts"] == 1
    clone_audit = task["commands"][-1]
    assert clone_audit["label"] == "clone-retry"
    assert "secret-token" not in str(clone_audit)
    assert repo.joinpath("partial").read_text(encoding="utf-8") == "preserve"
    assert saved


def test_non_transient_failed_initialization_is_not_recloned(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "code_123456abcdef"
    task = {
        "id": "code_123456abcdef",
        "status": "error",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": "feature/test",
        "workspace_path": str(workspace),
        "repo_path": str(workspace / "repo"),
        "commands": [
            {
                "label": "clone",
                "ok": False,
                "stderr_tail": "fatal: Authentication failed",
                "stdout_tail": "",
            }
        ],
    }
    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
        task_workspace_lock=cw.task_workspace_lock,
    )

    with pytest.raises(HTTPException) as excinfo:
        nr.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert "non-transient" in str(excinfo.value.detail)
