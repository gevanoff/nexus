from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app import coding_mission_acceptance_integrity as integrity


class _Guarded:
    _mission_acceptance_integrity_installed = False


class _Agent:
    def __init__(self, cw):
        self.cw = cw
        self.calls = []

    def finalize_successful_run(self, task_id, **kwargs):
        self.calls.append((task_id, kwargs))
        self.cw.current_head = "final-commit"
        return {"ok": True, "final_commit": "final-commit"}


class _CW:
    def __init__(self):
        self.current_head = "workspace-head"
        self.task = {
            "id": "code-test",
            "agent_start_head": "older-run-head",
            "agent_run_id": "run-b",
            "coding_mission_acceptance_epoch": {
                "schema": "nexus_coding_mission_acceptance_epoch.v1",
                "status": "semantic_accepted",
                "base_head": "mission-base",
                "accepted_fingerprint": "fingerprint",
                "accepted_head": "workspace-head",
                "finalized_head": "",
            },
        }

    def git_head(self, _task_id):
        return {"ok": True, "commit": self.current_head}

    def git_change_summary(self, _task_id):
        return {"ok": True, "counts": {"total": 0}}

    def load_task(self, _task_id):
        return self.task

    def save_task(self, task):
        self.task = task
        return task

    def mutate_task(self, _task_id, apply):
        apply(self.task)
        return self.task


def _epoch_facade():
    facade = SimpleNamespace()
    facade.KEY = "coding_mission_acceptance_epoch"
    facade.SCHEMA = "nexus_coding_mission_acceptance_epoch.v1"
    facade._MAX_UNTRACKED = 200
    facade._MAX_UNTRACKED_BYTES = 100_000
    facade._resolve_acceptance_base = lambda _cw, _task_id, _task: "merge-base"
    facade._safe_untracked_diff = lambda _cw, *, repo: (str(repo), "")
    facade._run_process = lambda cw, argv, *, cwd: cw._run_process(argv, cwd=cwd)
    facade.mission_delta_state = lambda _cw, _task_id, _task: {
        "ok": False,
        "has_delta": False,
    }
    facade._epoch_accepted_for_current = lambda *_args, **_kwargs: False
    return facade


def test_untracked_binary_fingerprint_binds_exact_bytes(tmp_path: Path):
    path = tmp_path / "asset.bin"
    path.write_bytes(b"\x00abc")

    class CW:
        @staticmethod
        def _run_process(argv, *, cwd):
            assert cwd == tmp_path
            assert argv == ["git", "ls-files", "--others", "--exclude-standard"]
            return {"ok": True, "stdout": "asset.bin\n", "stderr": ""}

    epoch = _epoch_facade()
    first, error = integrity._content_bound_untracked_diff(epoch, CW(), repo=tmp_path)
    assert not error
    assert "sha256=" in first
    assert "Binary untracked file" in first

    # Same path and byte length, different contents: the semantic identity must change.
    path.write_bytes(b"\x00abd")
    second, error = integrity._content_bound_untracked_diff(epoch, CW(), repo=tmp_path)
    assert not error
    assert first != second


def test_fresh_workspace_uses_exact_pre_agent_head_but_legacy_uses_merge_base():
    cw = _CW()
    epoch = _epoch_facade()
    agent = _Agent(cw)
    integrity.install(epoch, agent, _Guarded(), cw, SimpleNamespace())

    fresh = {"id": "code-test", "base_branch": "main"}
    assert epoch._resolve_acceptance_base(cw, "code-test", fresh) == "workspace-head"

    legacy = {"id": "code-test", "base_branch": "main", "agent_run_id": "run-a"}
    assert epoch._resolve_acceptance_base(cw, "code-test", legacy) == "merge-base"


def test_successful_normal_finalization_records_commit_created_by_finalizer():
    cw = _CW()
    epoch = _epoch_facade()
    agent = _Agent(cw)
    integrity.install(epoch, agent, _Guarded(), cw, SimpleNamespace())

    result = agent.finalize_successful_run("code-test", mission={})
    assert result["ok"] is True
    stored = cw.task[epoch.KEY]
    assert stored["accepted_head"] == "final-commit"
    assert stored["finalized_head"] == "final-commit"
    assert stored["status"] == "finalized"


def test_inherited_finalization_fails_closed_when_head_changes_before_publish():
    cw = _CW()
    cw.task["agent_start_head"] = "workspace-head"
    cw.task["coding_mission"] = {
        "completion_policy": {
            "require_validation_after_edit": True,
            "require_diff_review_after_edit": True,
            "require_commit_on_success": True,
        },
        "publish_policy": {"push": "on_success"},
    }
    pushed = []

    def git_status(_task_id, git_token_value=None):
        del git_token_value
        return {"ok": True}

    cw.git_status = git_status
    cw.git_diff = lambda _task_id: {"ok": True}
    cw.coding_state_snapshot = lambda _task_id: {
        "validation": {"validation_after_latest_edit": True, "last_validation_ok": True},
        "diff_review": {"diff_reviewed_after_latest_edit": True},
    }
    cw.normalize_coding_mission = lambda task, mission=None: dict(mission or task["coding_mission"])
    cw.push_task = lambda *args, **kwargs: pushed.append((args, kwargs)) or {"ok": True}

    epoch = _epoch_facade()
    epoch.mission_delta_state = lambda _cw, _task_id, _task: {
        "ok": True,
        "has_delta": True,
        "current_head": "workspace-head",
    }
    epoch._epoch_accepted_for_current = lambda *_args, **_kwargs: True

    # The accepted state said workspace-head, but the repository moves before the
    # final audit reads HEAD. Publication must stop before push.
    cw.current_head = "concurrent-head"
    result = integrity._finalize_inherited_delta(
        epoch=epoch,
        terminal_hardening=SimpleNamespace(),
        cw=cw,
        agent=SimpleNamespace(),
        task_id="code-test",
        mission=cw.task["coding_mission"],
        git_token_value=None,
        finish_summary="finish",
    )
    assert result["ok"] is False
    assert "HEAD changed" in result["finalization_error"]
    assert pushed == []
