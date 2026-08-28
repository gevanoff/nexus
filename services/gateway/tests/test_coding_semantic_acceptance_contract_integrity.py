from __future__ import annotations

from types import SimpleNamespace

from app import coding_semantic_acceptance
from app import coding_semantic_acceptance_contract as contract


class _MemoryCW:
    def __init__(self, task: dict):
        self.task = dict(task)

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def save_task(self, task: dict) -> dict:
        self.task = dict(task)
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        latest = dict(self.task)
        apply(latest)
        self.task = latest
        return dict(latest)


def _install(*, task: dict, epoch, record_rejection=None):
    cw = _MemoryCW(task)
    agent = SimpleNamespace(
        _coding_live_refutation_execution_installed=True,
        _append_event=lambda _task_id, _event: None,
        _clip_text=lambda text, _limit: text,
    )

    async def original_review(_task_id, _task, *, diff_text):
        raise AssertionError(f"unexpected reviewer call for {diff_text}")

    guarded = SimpleNamespace(_semantic_acceptance_review=original_review)
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"base:{diff_text}"
    )
    if record_rejection is not None:
        terminal._record_rejection = record_rejection
    debug = SimpleNamespace(
        _event_view=lambda event: dict(event),
        collect_debug_snapshot=lambda _task_id, active_runner=None: {},
        redact_text=lambda value, limit=2400: str(value)[:limit],
        _sanitize=lambda value: value,
    )
    contract.install(
        agent,
        guarded,
        cw,
        epoch,
        terminal,
        coding_semantic_acceptance,
        debug,
    )
    return cw, agent, guarded, terminal


def test_rejection_ledger_uses_fingerprint_from_actual_review() -> None:
    recorded: list[str] = []

    def record_rejection(_agent, _task_id, _task, *, fingerprint, result):
        assert result["semantic_review"]["fingerprint"] == "reviewed-contract-fingerprint"
        recorded.append(fingerprint)

    epoch = SimpleNamespace()
    cw, agent, _guarded, terminal = _install(
        task={"id": "code_rejection_race", "prompt": "Do the work."},
        epoch=epoch,
        record_rejection=record_rejection,
    )

    terminal._record_rejection(
        agent,
        "code_rejection_race",
        cw.load_task("code_rejection_race"),
        fingerprint="stale-pre-review-fingerprint",
        result={
            "semantic_review": {
                "fingerprint": "reviewed-contract-fingerprint",
                "review_error": False,
            }
        },
    )

    assert recorded == ["reviewed-contract-fingerprint"]


def test_mission_acceptance_refuses_accepted_event_for_stale_fingerprint() -> None:
    state = {"review": {"accepted": True, "fingerprint": "stale"}, "calls": 0}

    def latest_accepted_review(_task):
        return dict(state["review"])

    def mission_review_diff(_cw, _agent, _task_id, _task):
        return "review-diff"

    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id):
        state["calls"] += 1

    epoch = SimpleNamespace(
        KEY="coding_mission_acceptance_epoch",
        SCHEMA="nexus_coding_mission_acceptance_epoch.v1",
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    cw, agent, _guarded, terminal = _install(
        task={"id": "code_accept_race", "prompt": "Do the work."},
        epoch=epoch,
    )

    epoch._record_semantic_acceptance(terminal, cw, agent, "code_accept_race")
    assert state["calls"] == 0

    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        cw.load_task("code_accept_race"),
        diff_text="review-diff",
    )
    epoch._record_semantic_acceptance(terminal, cw, agent, "code_accept_race")
    assert state["calls"] == 1


def test_mission_acceptance_clears_state_if_workspace_changes_during_record() -> None:
    state = {"review": {"accepted": True, "fingerprint": ""}}

    def latest_accepted_review(_task):
        return dict(state["review"])

    def mission_review_diff(_cw, _agent, _task_id, task):
        return f"review-diff-v{int(task.get('repo_version') or 1)}"

    def record_semantic_acceptance(terminal, cw, _agent, task_id):
        before = cw.load_task(task_id)
        accepted_fp = terminal.semantic_acceptance_fingerprint(
            before,
            diff_text=mission_review_diff(cw, None, task_id, before),
        )

        def apply(latest):
            latest["coding_mission_acceptance_epoch"] = {
                "schema": "nexus_coding_mission_acceptance_epoch.v1",
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "head-v1",
                "accepted_run_id": "run-v1",
                "accepted_fingerprint": accepted_fp,
                "accepted_diff_sha256": "diff-v1",
            }
            # Simulate a repository/controller mutation landing after review but
            # before the legacy acceptance recorder returns.
            latest["repo_version"] = 2

        cw.mutate_task(task_id, apply)

    epoch = SimpleNamespace(
        KEY="coding_mission_acceptance_epoch",
        SCHEMA="nexus_coding_mission_acceptance_epoch.v1",
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    cw, agent, _guarded, terminal = _install(
        task={
            "id": "code_accept_post_race",
            "prompt": "Do the work.",
            "repo_version": 1,
        },
        epoch=epoch,
    )
    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        cw.load_task("code_accept_post_race"),
        diff_text="review-diff-v1",
    )

    epoch._record_semantic_acceptance(terminal, cw, agent, "code_accept_post_race")

    accepted = cw.task["coding_mission_acceptance_epoch"]
    assert cw.task["repo_version"] == 2
    assert accepted["status"] == "pending"
    assert accepted["accepted_fingerprint"] == ""
    assert accepted["accepted_head"] == ""
    assert accepted["accepted_diff_sha256"] == ""
