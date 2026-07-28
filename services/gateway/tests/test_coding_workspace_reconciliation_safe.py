from __future__ import annotations

from app import coding_workspace_reconciliation_safe as reconciliation


def _task(**overrides):
    value = {
        "id": "code_abcdef123456",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "repo_path": "/tmp/repo",
        "base_branch": "main",
        "agent_status": "paused",
        "last_checkpoint_commit": "workspace-head",
        "last_pr_output": "https://github.com/gevanoff/nexus/pull/50",
    }
    value.update(overrides)
    return value


def _merged_pr(head_sha="pr-head"):
    return {
        "ok": True,
        "body": {
            "state": "closed",
            "merged_at": "2026-07-27T23:00:00Z",
            "html_url": "https://github.com/gevanoff/nexus/pull/50",
            "merge_commit_sha": "merge-sha",
            "head": {"sha": head_sha},
        },
    }


def test_advanced_workspace_after_merge_remains_resumable(monkeypatch):
    task = _task(last_checkpoint_commit="new-work")
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(reconciliation.cw, "git_head", lambda _task_id: {"ok": True, "commit": "new-work"})
    monkeypatch.setattr(reconciliation.cw, "_github_api_request", lambda *args, **kwargs: _merged_pr("old-pr-head"))
    monkeypatch.setattr(
        reconciliation.base,
        "_local_integration_state",
        lambda *args, **kwargs: {
            "known": True,
            "integrated": False,
            "source": "git_patch_equivalence",
            "ahead_commits": 1,
        },
    )

    result = reconciliation.reconcile_task_before_run("code_abcdef123456")

    assert result["proceed"] is True
    assert result["status"] == "post_merge_changes"
    assert result["evidence"]["reason"] == "workspace_head_advanced_after_merged_pull_request"


def test_workspace_at_merged_pr_head_is_terminal(monkeypatch):
    task = _task(last_checkpoint_commit="pr-head")
    stored = dict(task)
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(reconciliation.cw, "git_head", lambda _task_id: {"ok": True, "commit": "pr-head"})
    monkeypatch.setattr(reconciliation.cw, "_github_api_request", lambda *args, **kwargs: _merged_pr("pr-head"))
    monkeypatch.setattr(
        reconciliation.base,
        "_mark_integrated",
        lambda task_id, evidence, actor: stored | {"agent_status": "completed", "evidence": evidence},
    )

    result = reconciliation.reconcile_task_before_run("code_abcdef123456")

    assert result["proceed"] is False
    assert result["status"] == "integrated"
    assert result["task"]["agent_status"] == "completed"


def test_merged_pr_with_unknown_workspace_relationship_fails_open(monkeypatch):
    task = _task(last_checkpoint_commit="")
    monkeypatch.setattr(reconciliation.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(reconciliation.cw, "git_head", lambda _task_id: {"ok": False, "commit": ""})
    monkeypatch.setattr(reconciliation.cw, "_github_api_request", lambda *args, **kwargs: _merged_pr("pr-head"))
    monkeypatch.setattr(
        reconciliation.base,
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
