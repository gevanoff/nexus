from __future__ import annotations

from types import SimpleNamespace

from app import coding_run_delta


class FakeWorkspace:
    def __init__(self) -> None:
        self.saved: dict | None = None
        self.commands: list[list[str]] = []
        self.files: dict[str, str] = {}
        self.responses: dict[tuple[str, ...], dict] = {}
        self.task = {"id": "code_test", "agent_run_id": "run-1"}

    def git_head(self, _task_id: str) -> dict:
        return {"commit": "head123"}

    def run_task_command(self, _task_id: str, *, argv, timeout_sec=30) -> dict:
        key = tuple(str(item) for item in argv)
        self.commands.append(list(key))
        return dict(self.responses.get(key, {"ok": False, "error": f"unexpected command: {key}"}))

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def save_task(self, task: dict) -> dict:
        self.saved = dict(task)
        self.task = dict(task)
        return task

    def read_file(self, _task_id: str, *, path: str) -> dict:
        if path not in self.files:
            raise FileNotFoundError(path)
        return {"path": path, "content": self.files[path]}


def test_capture_baseline_snapshots_preexisting_untracked_as_git_blobs() -> None:
    cw = FakeWorkspace()
    cw.responses[("git", "stash", "create", "nexus-semantic-acceptance-baseline")] = {
        "ok": True,
        "stdout": "baseline456\n",
    }
    cw.responses[("git", "ls-files", "--others", "--exclude-standard")] = {
        "ok": True,
        "stdout": "existing.txt\n",
    }
    cw.responses[("git", "hash-object", "-w", "--", "existing.txt")] = {
        "ok": True,
        "stdout": "blob789\n",
    }

    baseline = coding_run_delta.ensure_baseline(cw, "code_test", cw.task)

    assert baseline["tree_commit"] == "baseline456"
    assert baseline["untracked_blobs"] == {"existing.txt": "blob789"}
    assert cw.saved is not None
    assert cw.saved["agent_semantic_baseline"] == baseline


def test_errored_same_run_baseline_is_retried() -> None:
    cw = FakeWorkspace()
    cw.task["agent_semantic_baseline"] = {
        "schema": coding_run_delta.SCHEMA,
        "run_id": "run-1",
        "tree_commit": "",
        "untracked_blobs": {},
        "error": "transient git failure",
    }
    cw.responses[("git", "stash", "create", "nexus-semantic-acceptance-baseline")] = {
        "ok": True,
        "stdout": "baseline456\n",
    }
    cw.responses[("git", "ls-files", "--others", "--exclude-standard")] = {
        "ok": True,
        "stdout": "",
    }

    baseline = coding_run_delta.ensure_baseline(cw, "code_test", cw.task)

    assert baseline["error"] == ""
    assert baseline["tree_commit"] == "baseline456"
    assert ["git", "stash", "create", "nexus-semantic-acceptance-baseline"] in cw.commands


def test_run_delta_excludes_preexisting_workspace_changes_and_includes_new_untracked() -> None:
    cw = FakeWorkspace()
    task = {
        "agent_run_id": "run-1",
        "agent_semantic_baseline": {
            "schema": coding_run_delta.SCHEMA,
            "run_id": "run-1",
            "tree_commit": "baseline456",
            "untracked_blobs": {"existing.txt": "blob789"},
            "error": "",
        },
    }
    diff_command = (
        "git",
        "diff",
        "--no-ext-diff",
        "--binary",
        "baseline456",
        "--",
        ".",
        ":(exclude,literal)existing.txt",
    )
    cw.responses[diff_command] = {
        "ok": True,
        "stdout": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@\n+run edit\n",
    }
    cw.responses[("git", "ls-files", "--others", "--exclude-standard")] = {
        "ok": True,
        "stdout": "existing.txt\nnew_file.py\n",
    }
    cw.responses[("git", "hash-object", "--", "existing.txt")] = {
        "ok": True,
        "stdout": "blob789\n",
    }
    cw.files["existing.txt"] = "prior content\n"
    cw.files["new_file.py"] = "print('new')\n"
    agent = SimpleNamespace(_clip_text=lambda value, _limit: value)

    rendered = coding_run_delta.run_delta_diff(cw, agent, "code_test", task)

    assert "run edit" in rendered
    assert "new_file.py" in rendered
    assert "print('new')" in rendered
    assert "prior content" not in rendered
    assert list(diff_command) in cw.commands


def test_run_delta_diffs_changed_file_that_was_untracked_before_run() -> None:
    cw = FakeWorkspace()
    task = {
        "agent_run_id": "run-1",
        "agent_semantic_baseline": {
            "schema": coding_run_delta.SCHEMA,
            "run_id": "run-1",
            "tree_commit": "baseline456",
            "untracked_blobs": {"existing.txt": "oldblob"},
            "error": "",
        },
    }
    diff_command = (
        "git",
        "diff",
        "--no-ext-diff",
        "--binary",
        "baseline456",
        "--",
        ".",
        ":(exclude,literal)existing.txt",
    )
    cw.responses[diff_command] = {"ok": True, "stdout": ""}
    cw.responses[("git", "ls-files", "--others", "--exclude-standard")] = {
        "ok": True,
        "stdout": "existing.txt\n",
    }
    cw.responses[("git", "hash-object", "--", "existing.txt")] = {
        "ok": True,
        "stdout": "newblob\n",
    }
    cw.responses[("git", "cat-file", "blob", "oldblob")] = {
        "ok": True,
        "stdout": "before\n",
    }
    cw.files["existing.txt"] = "after\n"
    agent = SimpleNamespace(_clip_text=lambda value, _limit: value)

    rendered = coding_run_delta.run_delta_diff(cw, agent, "code_test", task)

    assert "-before" in rendered
    assert "+after" in rendered
