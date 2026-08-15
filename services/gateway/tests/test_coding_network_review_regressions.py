from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from urllib import error as urlerror

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_model_metadata_resilience as metadata_resilience
from app import coding_network_resilience as network_resilience


def _transient_clone_task(workspace_root: Path, *, repo_name: str = "repo"):
    task_id = "code_123456abcdef"
    workspace = workspace_root / task_id
    return {
        "id": task_id,
        "status": "error",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": f"nexus-coder/{task_id}",
        "workspace_path": str(workspace),
        "repo_path": str(workspace / repo_name),
        "commands": [
            {
                "label": "clone",
                "ok": False,
                "stderr_tail": "fatal: unable to access repository: Could not resolve host: github.com",
                "stdout_tail": "",
            }
        ],
    }


def test_generic_timeout_text_is_not_classified_as_network_failure():
    assert network_resilience.classify_transient_text("local lock timeout") == ""
    assert network_resilience.classify_transient_text("test harness timed out") == ""
    assert network_resilience.classify_transient_text("connection timed out") == "timeout"
    assert network_resilience.classify_transient_text("operation timed out") == "timeout"


def test_metadata_retry_does_not_capture_base_exceptions():
    calls = 0

    def original(model_id: str, *, timeout_sec: float = 10.0):
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt("stop now")

    with pytest.raises(KeyboardInterrupt):
        metadata_resilience.fetch_metadata_with_retry(
            original,
            "example/model",
            sleep_fn=lambda _: None,
            attempts=4,
            base_delay_sec=0,
        )

    assert calls == 1


def test_metadata_retry_does_not_retry_unclassified_urlerror():
    calls = 0

    def original(model_id: str, *, timeout_sec: float = 10.0):
        nonlocal calls
        calls += 1
        raise urlerror.URLError("unsupported local request composition")

    with pytest.raises(urlerror.URLError):
        metadata_resilience.fetch_metadata_with_retry(
            original,
            "example/model",
            sleep_fn=lambda _: None,
            attempts=4,
            base_delay_sec=0,
        )

    assert calls == 1


def test_persisted_recovery_requires_exact_task_repo_layout(tmp_path):
    workspace_root = tmp_path / "workspaces"
    task = _transient_clone_task(workspace_root, repo_name="unexpected")
    repo_path = Path(task["repo_path"])
    repo_path.mkdir(parents=True)
    sentinel = repo_path / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
    )

    with pytest.raises(HTTPException) as excinfo:
        network_resilience.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert "controller-owned" in str(excinfo.value.detail)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_persisted_recovery_reports_partial_clone_cleanup_failure(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspaces"
    task = _transient_clone_task(workspace_root)
    repo_path = Path(task["repo_path"])
    repo_path.mkdir(parents=True)
    saved = []

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
        save_task=lambda current: saved.append(dict(current)) or current,
    )

    def fail_cleanup(path):
        raise PermissionError("read-only mount")

    monkeypatch.setattr(network_resilience.shutil, "rmtree", fail_cleanup)

    with pytest.raises(HTTPException) as excinfo:
        network_resilience.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert "could not safely remove" in str(excinfo.value.detail)
    assert saved
    assert saved[-1]["initialization_recovery"]["recovered"] is False
    assert "PermissionError" in saved[-1]["initialization_recovery"]["cleanup_error"]
