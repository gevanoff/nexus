from __future__ import annotations

import json
import os
import subprocess

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from gateway.app import coding_workspace as cw


def _git(repo, *args):
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Test User")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Test User")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    subprocess.run(["git", *args], cwd=repo, check=True, env=env, capture_output=True, text=True)


def test_git_base_branch_diff_includes_committed_workspace_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "branch", "-M", "main")

    readme = repo / "README.md"
    readme.write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")

    _git(repo, "switch", "-c", "feature/test")
    readme.write_text("hello\nworld\n", encoding="utf-8")
    _git(repo, "commit", "-am", "update readme")

    result = cw._git_base_branch_diff(repo, base_branch="main")

    assert result["ok"] is True
    assert result["base_ref"] == "main"
    assert result["compare_ref"]
    assert "README.md" in str(result["stat"].get("stdout") or "")
    assert result["changes"]["counts"]["total"] == 1
    assert result["changes"]["files"][0]["path"] == "README.md"
    assert "+world" in str(result["diff"].get("stdout") or "")
    assert result["committed_changes"]["counts"]["total"] == 1
    assert "+world" in str(result["committed_diff"].get("stdout") or "")


def test_git_diff_returns_base_branch_metadata_for_task(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspaces"
    tasks_root = tmp_path / "tasks"
    workspace_root.mkdir()
    tasks_root.mkdir()

    monkeypatch.setattr(cw, "workspace_root", lambda: workspace_root)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tasks_root)

    task_id = "code_abcdef123456"
    repo = workspace_root / task_id / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "branch", "-M", "main")

    app_file = repo / "app.py"
    app_file.write_text("print('base')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")
    _git(repo, "switch", "-c", "feature/task")
    app_file.write_text("print('changed')\n", encoding="utf-8")
    _git(repo, "commit", "-am", "change")

    task_payload = {
        "schema": cw.SCHEMA,
        "id": task_id,
        "status": "ready",
        "created_at": 0,
        "updated_at": 0,
        "owner": "test",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "branch_name": "feature/task",
        "workspace_path": str(workspace_root / task_id),
        "repo_path": str(repo),
        "commands": [],
    }
    (tasks_root / f"{task_id}.json").write_text(json.dumps(task_payload), encoding="utf-8")

    result = cw.git_diff(task_id)

    assert result["ok"] is True
    assert result["scope"] == "base_branch"
    assert result["base_branch"] == "main"
    assert result["branch_name"] == "feature/task"
    assert result["compare_ref"]
    assert result["changes"]["counts"]["total"] == 1
    assert result["changes"]["files"][0]["path"] == "app.py"
    assert result["committed_changes"]["counts"]["total"] == 1
    assert "+print('changed')" in str(result["diff"].get("stdout") or "")