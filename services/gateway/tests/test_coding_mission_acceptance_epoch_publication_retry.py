from __future__ import annotations

from types import SimpleNamespace

from app import coding_mission_acceptance_epoch as epoch


class _RaceCW:
    def __init__(
        self,
        task: dict,
        *,
        mutate_workspace_on_first_cas: bool = False,
        clear_stale_on_second_cas: bool = False,
        mutate_epoch_on_second_cas: bool = False,
        replacement_fingerprint_on_second_cas: str = "",
    ):
        self.task = dict(task)
        self.mutations = 0
        self.mutate_workspace_on_first_cas = mutate_workspace_on_first_cas
        self.clear_stale_on_second_cas = clear_stale_on_second_cas
        self.mutate_epoch_on_second_cas = mutate_epoch_on_second_cas
        self.replacement_fingerprint_on_second_cas = replacement_fingerprint_on_second_cas

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
        elif self.mutations == 2 and self.clear_stale_on_second_cas:
            # A's post-publication stale cleanup wins after B has reloaded A's
            # fingerprint but before B enters its final CAS callback.
            latest[epoch.KEY] = {
                **dict(latest[epoch.KEY]),
                "status": "pending",
                "accepted_at": 0.0,
                "accepted_head": "",
                "accepted_run_id": "",
                "accepted_fingerprint": "",
                "accepted_diff_sha256": "",
            }
            if self.mutate_epoch_on_second_cas:
                # A real workspace mutation also clears acceptance, but unlike
                # stale cleanup it advances the epoch's mutation identity.
                latest[epoch.KEY] = {
                    **dict(latest[epoch.KEY]),
                    "last_mutation_at": 99.0,
                    "last_mutation_run_id": "run-mutator",
                }
        elif self.mutations == 2 and self.replacement_fingerprint_on_second_cas:
            # A different non-empty publication is never a cleanup transition;
            # B must not overwrite it merely because B is on its retry attempt.
            latest[epoch.KEY] = {
                **dict(latest[epoch.KEY]),
                "status": "semantic_accepted",
                "accepted_at": 2.0,
                "accepted_head": "other-head",
                "accepted_run_id": "other-run",
                "accepted_fingerprint": self.replacement_fingerprint_on_second_cas,
                "accepted_diff_sha256": "other-sha",
            }
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
            "last_mutation_at": 1.0,
            "last_mutation_run_id": "run-before-review",
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
        reviewed_fingerprint="fp:current-diff",
        reviewed_cycle=4,
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
        reviewed_fingerprint="fp:current-diff",
        reviewed_cycle=4,
    )

    assert published is False
    # The second attempt reloads/revalidates and exits before a second mutation.
    assert cw.mutations == 1
    assert cw.task["repo_version"] == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["accepted_fingerprint"] == "fp:stale-diff"
    assert accepted["accepted_head"] == "stale-head"


def test_retry_publishes_when_observed_stale_acceptance_is_cleared_before_final_cas(
    monkeypatch,
) -> None:
    _install_delta_stubs(monkeypatch)
    cw = _RaceCW(_task(), clear_stale_on_second_cas=True)
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        SimpleNamespace(),
        "code_epoch_publication_retry",
        reviewed_fingerprint="fp:current-diff",
        reviewed_cycle=4,
    )

    assert published is True
    assert cw.mutations == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:current-diff"
    assert accepted["accepted_head"] == "head-v1"
    assert accepted["accepted_run_id"] == "run-current"
    assert accepted["accepted_diff_sha256"] == "sha-v1"


def test_retry_does_not_treat_workspace_mutation_clear_as_stale_cleanup(
    monkeypatch,
) -> None:
    _install_delta_stubs(monkeypatch)
    cw = _RaceCW(
        _task(),
        clear_stale_on_second_cas=True,
        mutate_epoch_on_second_cas=True,
    )
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        SimpleNamespace(),
        "code_epoch_publication_retry",
        reviewed_fingerprint="fp:current-diff",
        reviewed_cycle=4,
    )

    assert published is False
    assert cw.mutations == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "pending"
    assert accepted["accepted_fingerprint"] == ""
    assert accepted["last_mutation_at"] == 99.0
    assert accepted["last_mutation_run_id"] == "run-mutator"


def test_retry_does_not_overwrite_different_nonempty_acceptance_on_final_cas(
    monkeypatch,
) -> None:
    _install_delta_stubs(monkeypatch)
    cw = _RaceCW(
        _task(),
        replacement_fingerprint_on_second_cas="fp:other-current",
    )
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        SimpleNamespace(),
        "code_epoch_publication_retry",
        reviewed_fingerprint="fp:current-diff",
        reviewed_cycle=4,
    )

    assert published is False
    assert cw.mutations == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == "fp:other-current"
    assert accepted["accepted_head"] == "other-head"
    assert accepted["accepted_run_id"] == "other-run"
    assert accepted["accepted_diff_sha256"] == "other-sha"
