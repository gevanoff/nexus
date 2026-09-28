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
from app import coding_workspace


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


def test_persisted_recovery_rejects_repo_symlink_without_touching_target(tmp_path):
    workspace_root = tmp_path / "workspaces"
    task = _transient_clone_task(workspace_root)
    repo_path = Path(task["repo_path"])
    repo_path.parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    repo_path.symlink_to(external, target_is_directory=True)

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
    )

    with pytest.raises(HTTPException) as excinfo:
        network_resilience.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert "symbolic links" in str(excinfo.value.detail)
    assert repo_path.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_persisted_recovery_does_not_follow_swapped_workspace_parent(tmp_path):
    workspace_root = tmp_path / "workspaces"
    task = _transient_clone_task(workspace_root)
    workspace_path = Path(task["workspace_path"])
    repo_path = Path(task["repo_path"])
    workspace_path.mkdir(parents=True)
    original_workspace = tmp_path / "original-workspace"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    saved = []

    def run_process(argv, *, cwd, pass_fds=(), **_kwargs):
        assert pass_fds
        if argv[1] == "clone":
            workspace_path.rename(original_workspace)
            workspace_path.symlink_to(external, target_is_directory=True)
            anchored_repo = Path(argv[-1])
            anchored_repo.mkdir()
            anchored_repo.joinpath(".git").mkdir()
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
        save_task=lambda current: saved.append(dict(current)) or current,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(HTTPException) as excinfo:
        network_resilience.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert "path changed" in str(excinfo.value.detail)
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not external.joinpath("repo").exists()
    assert original_workspace.joinpath("repo", ".git").is_dir()


def test_persisted_recovery_does_not_follow_repo_swapped_after_clone(tmp_path):
    workspace_root = tmp_path / "workspaces"
    task = _transient_clone_task(workspace_root)
    workspace_path = Path(task["workspace_path"])
    workspace_path.mkdir(parents=True)
    preserved_clone = tmp_path / "preserved-clone"
    external_repo = tmp_path / "external-repo"
    external_repo.joinpath(".git").mkdir(parents=True)
    sentinel = external_repo / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    commands = []

    def run_process(argv, *, cwd, pass_fds=(), **_kwargs):
        assert pass_fds
        commands.append(argv[1])
        if argv[1] == "clone":
            anchored_repo = Path(argv[-1])
            anchored_repo.mkdir()
            anchored_repo.joinpath(".git").mkdir()
            anchored_repo.rename(preserved_clone)
            anchored_repo.symlink_to(external_repo, target_is_directory=True)
        else:
            external_repo.joinpath("mutated.txt").write_text(
                "unsafe",
                encoding="utf-8",
            )
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
        save_task=lambda current: current,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(HTTPException) as excinfo:
        network_resilience.retry_failed_initialization(fake_cw, task["id"])

    assert excinfo.value.status_code == 409
    assert commands == ["clone"]
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not external_repo.joinpath("mutated.txt").exists()
    assert preserved_clone.joinpath(".git").is_dir()


def test_persisted_recovery_quarantines_partial_clone_on_same_task(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspaces"
    task = _transient_clone_task(workspace_root)
    repo_path = Path(task["repo_path"])
    repo_path.mkdir(parents=True)
    repo_path.joinpath(".git").mkdir()
    sentinel = repo_path / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    commands = []

    def run_process(argv, *, cwd, pass_fds=(), **_kwargs):
        assert pass_fds
        commands.append(argv[1])
        if argv[1] == "clone":
            anchored_repo = Path(argv[-1])
            anchored_repo.mkdir()
            anchored_repo.joinpath(".git").mkdir()
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    fake_cw = SimpleNamespace(
        load_task=lambda task_id: task,
        workspace_root=lambda: workspace_root,
        save_task=lambda current: current,
        command_timeout_sec=lambda value=None: 120.0,
        _run_process=run_process,
        _append_command=lambda *_args, **_kwargs: None,
    )

    recovered = network_resilience.retry_failed_initialization(
        fake_cw,
        task["id"],
    )

    assert recovered["status"] == "ready"
    assert commands == ["clone", "switch"]
    quarantine = workspace_root / task["id"] / recovered[
        "initialization_recovery"
    ]["partial_repo_quarantine"]
    assert quarantine.joinpath("keep.txt").read_text(encoding="utf-8") == "preserve"
    assert quarantine.joinpath(".git").is_dir()
    assert repo_path.joinpath(".git").is_dir()
    monkeypatch.setattr(coding_workspace, "workspace_root", lambda: workspace_root)
    assert coding_workspace._repo_path(recovered) == repo_path.resolve()

    recovered_repo = tmp_path / "recovered-repo"
    repo_path.rename(recovered_repo)
    external_repo = tmp_path / "handoff-external"
    external_repo.joinpath(".git").mkdir(parents=True)
    repo_path.symlink_to(external_repo, target_is_directory=True)

    with pytest.raises(HTTPException) as excinfo:
        coding_workspace._repo_path(recovered)

    assert excinfo.value.status_code == 409
    assert "identity" in str(excinfo.value.detail)
    assert recovered_repo.joinpath(".git").is_dir()
