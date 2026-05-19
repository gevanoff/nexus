from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from services.gateway.app import coding_workspace as cw


def test_git_commands_get_safe_directory_from_repo_root(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "subdir" / "deeper"
    (repo / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)

    argv = cw._argv_with_git_safe_directory(["git", "status", "--porcelain"], cwd=nested)

    assert argv[:3] == ["git", "-c", f"safe.directory={repo}"]
    assert argv[3:] == ["status", "--porcelain"]


def test_non_git_commands_are_unchanged(tmp_path):
    argv = cw._argv_with_git_safe_directory(["python", "-V"], cwd=tmp_path)

    assert argv == ["python", "-V"]