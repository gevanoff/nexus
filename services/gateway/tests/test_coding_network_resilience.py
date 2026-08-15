from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_network_resilience as nr


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


def test_clone_retries_transient_dns_and_removes_partial_destination(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "code_123456abcdef"
    destination = workspace / "repo"
    workspace.mkdir(parents=True)
    calls = []

    def original(argv, *, cwd, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            destination.mkdir(parents=True)
            (destination / ".git").mkdir()
            (destination / "partial").write_text("partial", encoding="utf-8")
            return _result(
                ok=False,
                stderr="fatal: unable to access 'https://github.com/example/repo.git/': Could not resolve host: github.com",
            )
        assert not destination.exists()
        destination.mkdir(parents=True)
        (destination / ".git").mkdir()
        return _result(ok=True)

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

    assert result["ok"] is True
    assert len(calls) == 2
    assert result["network_retry_count"] == 1
    assert result["network_retry_recovered"] is True
    assert result["network_retry_history"][0]["kind"] == "dns"


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
    )

    with pytest.raises(HTTPException) as excinfo:
        nr.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert "non-transient" in str(excinfo.value.detail)
