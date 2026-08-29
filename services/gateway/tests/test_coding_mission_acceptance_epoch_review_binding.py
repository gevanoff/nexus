from __future__ import annotations

from types import SimpleNamespace

from app import coding_mission_acceptance_epoch as epoch


class _MemoryCW:
    def __init__(self, task: dict):
        self.task = dict(task)
        self.mutations = 0

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        self.mutations += 1
        latest = dict(self.task)
        apply(latest)
        self.task = latest
        return dict(latest)


def _task(reviewed_fingerprint: str) -> dict:
    return {
        "id": "code_epoch_review_binding",
        "agent_cycle": 4,
        "agent_run_id": "run-4",
        "agent_events": [
            {
                "type": "semantic_acceptance_review",
                "cycle": 4,
                "accepted": True,
                "fingerprint": reviewed_fingerprint,
            }
        ],
        epoch.KEY: {
            "schema": epoch.SCHEMA,
            "status": "pending",
            "base_head": "base",
            "accepted_fingerprint": "",
        },
    }


def test_acceptance_recorder_refuses_live_fingerprint_not_bound_to_review(monkeypatch) -> None:
    cw = _MemoryCW(_task("fp:reviewed-diff"))
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )
    agent = SimpleNamespace()

    monkeypatch.setattr(
        epoch,
        "mission_delta_state",
        lambda _cw, _task_id, _task: {
            "ok": True,
            "has_delta": True,
            "base_head": "base",
            "current_head": "new-head",
            "diff_sha256": "new-diff-sha",
        },
    )
    monkeypatch.setattr(
        epoch,
        "mission_review_diff",
        lambda _cw, _agent, _task_id, _task: "newer-unreviewed-diff",
    )

    epoch._record_semantic_acceptance(terminal, cw, agent, "code_epoch_review_binding")

    assert cw.mutations == 0
    assert cw.task[epoch.KEY]["accepted_fingerprint"] == ""
    assert cw.task[epoch.KEY]["status"] == "pending"


def test_acceptance_recorder_publishes_only_matching_review_fingerprint(monkeypatch) -> None:
    cw = _MemoryCW(_task("fp:reviewed-diff"))
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )
    agent = SimpleNamespace()

    monkeypatch.setattr(
        epoch,
        "mission_delta_state",
        lambda _cw, _task_id, _task: {
            "ok": True,
            "has_delta": True,
            "base_head": "base",
            "current_head": "reviewed-head",
            "diff_sha256": "reviewed-diff-sha",
        },
    )
    monkeypatch.setattr(
        epoch,
        "mission_review_diff",
        lambda _cw, _agent, _task_id, _task: "reviewed-diff",
    )

    epoch._record_semantic_acceptance(terminal, cw, agent, "code_epoch_review_binding")

    assert cw.mutations == 1
    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:reviewed-diff"
    assert accepted["accepted_head"] == "reviewed-head"
    assert accepted["accepted_diff_sha256"] == "reviewed-diff-sha"
