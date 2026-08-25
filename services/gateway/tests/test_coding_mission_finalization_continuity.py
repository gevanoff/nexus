from __future__ import annotations

from app import coding_mission_finalization_continuity as finalization


class _Continuity:
    @staticmethod
    def _existing_epoch(task):
        return dict(task.get("coding_acceptance_epoch") or {})

    @staticmethod
    def _run_local_delta(_cw, _task_id, task):
        return task.get("agent_start_head") != task.get("last_commit")

    @staticmethod
    def mission_acceptance_state(_cw, _agent, _run_delta, _task_id, task):
        return {
            "has_delta": True,
            "base_head": "base",
            "current_head": task["last_commit"],
        }

    @staticmethod
    def _mark_epoch_accepted(cw, task_id, final_commit):
        task = cw.load_task(task_id)
        task["coding_acceptance_epoch"]["status"] = "accepted"
        task["coding_acceptance_epoch"]["accepted_head"] = final_commit
        cw.save_task(task)


class _CW:
    def __init__(self):
        self.task = {
            "id": "code-test",
            "agent_start_head": "checkpoint",
            "last_commit": "checkpoint",
            "last_checkpoint_at": 20.0,
            "updated_at": 21.0,
            "coding_acceptance_epoch": {
                "schema": "nexus_coding_acceptance_epoch.v1",
                "base_head": "base",
                "status": "pending",
            },
            "mission": {
                "completion_policy": {
                    "require_file_changes": True,
                    "require_commit_on_success": True,
                    "require_validation_after_edit": True,
                    "require_diff_review_after_edit": True,
                },
                "publish_policy": {
                    "push": "on_success",
                    "draft_pr": "on_success",
                    "remote": "origin",
                    "pr_title": "Mission PR",
                    "pr_body": "body",
                },
            },
        }
        self.seen_start_heads = []
        self.pushes = 0
        self.prs = 0

    def load_task(self, _task_id):
        return self.task

    def save_task(self, task):
        self.task = task
        return task

    def mutate_task(self, _task_id, mutator):
        mutator(self.task)
        return self.task

    def normalize_coding_mission(self, task, mission=None):
        return dict(mission or task["mission"])

    def git_status(self, _task_id, **_kwargs):
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {"ok": True}

    def git_diff(self, _task_id):
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {"ok": True, "diff": {"stdout": ""}}

    def git_change_summary(self, _task_id):
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {"ok": True, "counts": {"total": 0}, "files": []}

    def coding_state_snapshot(self, _task_id):
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {
            "validation": {
                "validation_after_latest_edit": True,
                "last_validation_ok": True,
            },
            "diff_review": {"diff_reviewed_after_latest_edit": True},
        }

    def git_head(self, _task_id):
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {"ok": True, "commit": self.task["last_commit"]}

    def push_task(self, _task_id, **_kwargs):
        self.pushes += 1
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {"ok": True}

    def create_pull_request(self, _task_id, **_kwargs):
        self.prs += 1
        self.seen_start_heads.append(self.task["agent_start_head"])
        return {"ok": True, "url": "https://example.test/pr/1", "number": 1}


class _Agent:
    def __init__(self):
        self.original_calls = 0

    def finalize_successful_run(self, _task_id, **_kwargs):
        self.original_calls += 1
        return {"ok": True, "final_commit": "original"}


class _Guarded:
    pass


class _RunDelta:
    pass


def test_inherited_finalization_never_rewrites_persisted_run_start_head():
    cw = _CW()
    agent = _Agent()
    guarded = _Guarded()

    finalization.install(agent, guarded, cw, _RunDelta(), _Continuity())
    result = agent.finalize_successful_run(
        "code-test",
        finish_summary="Fix complete",
        run_id="run-b",
    )

    assert result["ok"] is True
    assert result["final_commit"] == "checkpoint"
    assert agent.original_calls == 0
    assert cw.pushes == 1
    assert cw.prs == 1
    assert cw.task["agent_start_head"] == "checkpoint"
    assert set(cw.seen_start_heads) == {"checkpoint"}
    assert cw.task["coding_acceptance_epoch"]["status"] == "accepted"
    assert cw.task["coding_acceptance_epoch"]["accepted_head"] == "checkpoint"


def test_inherited_finalization_fails_closed_on_stale_validation():
    cw = _CW()
    cw.coding_state_snapshot = lambda _task_id: {
        "validation": {
            "validation_after_latest_edit": False,
            "last_validation_ok": True,
        },
        "diff_review": {"diff_reviewed_after_latest_edit": True},
    }
    agent = _Agent()
    guarded = _Guarded()
    finalization.install(agent, guarded, cw, _RunDelta(), _Continuity())

    result = agent.finalize_successful_run("code-test", finish_summary="Fix complete")

    assert result["ok"] is False
    assert result["finalization_status"] == "failed_finalization"
    assert "lacks validation" in result["finalization_error"]
    assert cw.pushes == 0
    assert cw.prs == 0
    assert cw.task["coding_acceptance_epoch"]["status"] == "pending"


def test_normal_run_local_delta_delegates_existing_finalizer():
    cw = _CW()
    cw.task["last_commit"] = "new-checkpoint"
    agent = _Agent()
    guarded = _Guarded()
    finalization.install(agent, guarded, cw, _RunDelta(), _Continuity())

    result = agent.finalize_successful_run("code-test", finish_summary="Fix complete")

    assert result == {"ok": True, "final_commit": "original"}
    assert agent.original_calls == 1
    assert cw.pushes == 0
