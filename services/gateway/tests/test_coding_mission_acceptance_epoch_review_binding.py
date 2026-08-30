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


def _owner_record_semantic_acceptance():
    return getattr(
        epoch,
        "_record_semantic_acceptance_before_semantic_contract",
        epoch._record_semantic_acceptance,
    )


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

    _owner_record_semantic_acceptance()(
        terminal,
        cw,
        agent,
        "code_epoch_review_binding",
        reviewed_fingerprint="fp:reviewed-diff",
        reviewed_cycle=4,
    )

    assert cw.mutations == 0
    assert cw.task[epoch.KEY]["accepted_fingerprint"] == ""
    assert cw.task[epoch.KEY]["status"] == "pending"


def test_explicit_finish_review_identity_beats_later_stale_shared_event(monkeypatch) -> None:
    cw = _MemoryCW(_task("fp:reviewed-diff"))
    cw.task["agent_events"].append(
        {
            "type": "semantic_acceptance_review",
            "cycle": 4,
            "accepted": True,
            "fingerprint": "fp:stale-diff",
        }
    )
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

    published = _owner_record_semantic_acceptance()(
        terminal,
        cw,
        agent,
        "code_epoch_review_binding",
        reviewed_fingerprint="fp:reviewed-diff",
        reviewed_cycle=4,
    )

    assert published is True
    assert cw.task[epoch.KEY]["accepted_fingerprint"] == "fp:reviewed-diff"


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

    _owner_record_semantic_acceptance()(
        terminal,
        cw,
        agent,
        "code_epoch_review_binding",
        reviewed_fingerprint="fp:reviewed-diff",
        reviewed_cycle=4,
    )

    assert cw.mutations == 1
    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:reviewed-diff"
    assert accepted["accepted_head"] == "reviewed-head"
    assert accepted["accepted_diff_sha256"] == "reviewed-diff-sha"


def test_owner_recorder_requires_complete_invocation_identity() -> None:
    # Shared review events are audit history; only the invocation-local token
    # carried by the finishing call can authorize a new acceptance publication.
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )
    recorder = _owner_record_semantic_acceptance()
    for kwargs in (
        {},
        {"reviewed_fingerprint": "fp:reviewed-diff"},
        {"reviewed_cycle": 4},
    ):
        cw = _MemoryCW(_task("fp:reviewed-diff"))
        published = recorder(
            terminal, cw, SimpleNamespace(), "code_epoch_review_binding", **kwargs
        )
        assert published is False
        assert cw.mutations == 0
        assert cw.task[epoch.KEY]["status"] == "pending"
        assert cw.task[epoch.KEY]["accepted_fingerprint"] == ""


def test_owner_recorder_rechecks_cycle_inside_atomic_publication(monkeypatch) -> None:
    # Cycle ownership is part of publication authority, so it must still hold
    # after the recorder crosses the final load-to-mutation boundary.
    class _CycleRaceCW(_MemoryCW):
        def mutate_task(self, _task_id: str, apply):
            self.mutations += 1
            latest = dict(self.task)
            latest["agent_cycle"] = 5
            apply(latest)
            self.task = latest
            return dict(latest)

    cw = _CycleRaceCW(_task("fp:reviewed-diff"))
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )
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
        epoch, "mission_review_diff",
        lambda _cw, _agent, _task_id, _task: "reviewed-diff",
    )
    published = _owner_record_semantic_acceptance()(
        terminal, cw, SimpleNamespace(), "code_epoch_review_binding",
        reviewed_fingerprint="fp:reviewed-diff", reviewed_cycle=4,
    )
    assert published is False
    assert cw.mutations == 1
    assert cw.task["agent_cycle"] == 5
    assert cw.task[epoch.KEY]["status"] == "pending"
    assert cw.task[epoch.KEY]["accepted_fingerprint"] == ""
