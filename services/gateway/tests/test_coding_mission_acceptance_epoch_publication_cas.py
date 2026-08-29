from __future__ import annotations

from types import SimpleNamespace

from app import coding_mission_acceptance_epoch as epoch


class _RaceCW:
    def __init__(self, task: dict, *, concurrent_fingerprint: str = ""):
        self.task = dict(task)
        self.concurrent_fingerprint = concurrent_fingerprint
        self.mutations = 0

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        self.mutations += 1
        latest = dict(self.task)
        if self.concurrent_fingerprint:
            # Recorder B wins the publication race after recorder A has checked
            # its reviewed fingerprint but before A acquires the mutation lock.
            latest[epoch.KEY] = {
                **dict(latest[epoch.KEY]),
                "status": "semantic_accepted",
                "accepted_at": 2.0,
                "accepted_head": "head-b",
                "accepted_run_id": "run-b",
                "accepted_fingerprint": self.concurrent_fingerprint,
                "accepted_diff_sha256": "diff-b",
            }
            self.concurrent_fingerprint = ""
        apply(latest)
        self.task = latest
        return dict(latest)


def _task(*, observed_acceptance: str = "") -> dict:
    return {
        "id": "code_epoch_publication_cas",
        "agent_cycle": 7,
        "agent_run_id": "run-a",
        "agent_events": [
            {
                "type": "semantic_acceptance_review",
                "cycle": 7,
                "accepted": True,
                "fingerprint": "fp:reviewed-a",
            }
        ],
        epoch.KEY: {
            "schema": epoch.SCHEMA,
            "status": "semantic_accepted" if observed_acceptance else "pending",
            "base_head": "base",
            "accepted_fingerprint": observed_acceptance,
            "accepted_head": "old-head" if observed_acceptance else "",
            "accepted_diff_sha256": "old-diff" if observed_acceptance else "",
        },
    }


def _bind_review_state(monkeypatch) -> None:
    monkeypatch.setattr(
        epoch,
        "mission_delta_state",
        lambda _cw, _task_id, _task: {
            "ok": True,
            "has_delta": True,
            "base_head": "base",
            "current_head": "head-a",
            "diff_sha256": "diff-a",
        },
    )
    monkeypatch.setattr(
        epoch,
        "mission_review_diff",
        lambda _cw, _agent, _task_id, _task: "reviewed-a",
    )


def test_recorder_does_not_overwrite_concurrent_acceptance(monkeypatch) -> None:
    _bind_review_state(monkeypatch)
    cw = _RaceCW(_task(), concurrent_fingerprint="fp:reviewed-b")
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    epoch._record_semantic_acceptance(terminal, cw, SimpleNamespace(), cw.task["id"])

    accepted = cw.task[epoch.KEY]
    assert cw.mutations == 1
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:reviewed-b"
    assert accepted["accepted_head"] == "head-b"
    assert accepted["accepted_diff_sha256"] == "diff-b"


def test_recorder_can_replace_exact_stale_acceptance_it_observed(monkeypatch) -> None:
    _bind_review_state(monkeypatch)
    cw = _RaceCW(_task(observed_acceptance="fp:stale"))
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    epoch._record_semantic_acceptance(terminal, cw, SimpleNamespace(), cw.task["id"])

    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:reviewed-a"
    assert accepted["accepted_head"] == "head-a"
    assert accepted["accepted_diff_sha256"] == "diff-a"
