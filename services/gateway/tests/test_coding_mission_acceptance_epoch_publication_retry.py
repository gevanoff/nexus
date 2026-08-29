from __future__ import annotations

from types import SimpleNamespace

from app import coding_mission_acceptance_epoch as epoch


class _RaceCW:
    def __init__(self, task: dict, *, mutate_workspace_on_first_cas: bool = False):
        self.task = dict(task)
        self.mutations = 0
        self.mutate_workspace_on_first_cas = mutate_workspace_on_first_cas

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        self.mutations += 1
        latest = dict(self.task)
        if self.mutations == 1:
            # Recorder A wins immediately before recorder B enters its first
            # compare-and-set callback. A is stale relative to B's accepted
            # review, but both originally observed an empty acceptance slot.
            latest[epoch.KEY] = {
                **dict(latest[epoch.KEY]),
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "stale-head",
                "accepted_run_id": "stale-run",
                "accepted_fingerprint": "fp:stale-diff",
                "accepted_diff_sha256": "stale-sha",
            }
            if self.mutate_workspace_on_first_cas:
                latest["repo_version"] = 2
        apply(latest)
        self.task = latest
        return dict(latest)


def _task() -> dict:
    return {
        "id": "code_epoch_publication_retry",
        "agent_cycle": 4,
        "agent_run_id": "run-current",
        "repo_version": 1,
        "agent_events": [
            {
                "type": "semantic_acceptance_review",
                "cycle": 4,
                "accepted": True,
                "fingerprint": "fp:current-diff",
            }
        ],
        epoch.KEY: {
            "schema": epoch.SCHEMA,
            "status": "pending",
            "base_head": "base",
            "accepted_fingerprint": "",
        },
    }


def _install_delta_stubs(monkeypatch) -> None:
    monkeypatch.setattr(
        epoch,
        "mission_delta_state",
        lambda _cw, _task_id, task: {
            "ok": True,
            "has_delta": True,
            "base_head": "base",
            "current_head": f"head-v{int(task.get('repo_version') or 1)}",
            "diff_sha256": f"sha-v{int(task.get('repo_version') or 1)}",
        },
    )
    monkeypatch.setattr(
        epoch,
        "mission_review_diff",
        lambda _cw, _agent, _task_id, task: (
            "current-diff"
            if int(task.get("repo_version") or 1) == 1
            else "newer-diff"
        ),
    )


def test_current_recorder_retries_after_stale_recorder_wins_first_cas(monkeypatch) -> None:
    _install_delta_stubs(monkeypatch)
    cw = _RaceCW(_task())
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        SimpleNamespace(),
        "code_epoch_publication_retry",
    )

    assert published is True
    assert cw.mutations == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:current-diff"
    assert accepted["accepted_head"] == "head-v1"
    assert accepted["accepted_run_id"] == "run-current"
    assert accepted["accepted_diff_sha256"] == "sha-v1"


def test_publication_retry_aborts_if_review_no_longer_matches_workspace(monkeypatch) -> None:
    _install_delta_stubs(monkeypatch)
    cw = _RaceCW(_task(), mutate_workspace_on_first_cas=True)
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        SimpleNamespace(),
        "code_epoch_publication_retry",
    )

    assert published is False
    # The second attempt reloads/revalidates and exits before a second mutation.
    assert cw.mutations == 1
    assert cw.task["repo_version"] == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["accepted_fingerprint"] == "fp:stale-diff"
    assert accepted["accepted_head"] == "stale-head"
