from __future__ import annotations

import asyncio
import logging
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


def _install(
    *,
    task: dict,
    epoch,
    record_rejection=None,
    prior_rejection=None,
    cw_override=None,
):
    cw = cw_override or _MemoryCW(task)
    agent = SimpleNamespace(
        _coding_live_refutation_execution_installed=True,
        _append_event=lambda _task_id, _event: None,
        _clip_text=lambda text, _limit: text,
    )

    async def original_review(_task_id, _task, *, diff_text):
        raise AssertionError(f"unexpected reviewer call for {diff_text}")

    guarded = SimpleNamespace(
        _semantic_acceptance_review=original_review,
        _run_delta_diff=lambda _task_id, _task: "review-diff",
    )
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"base:{diff_text}"
    )
    if record_rejection is not None:
        terminal._record_rejection = record_rejection
    if prior_rejection is not None:
        terminal._prior_rejection = prior_rejection
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


def test_frozen_contract_rejects_payload_tampering() -> None:
    task = {
        "id": "code_contract_integrity",
        "prompt": "Do the work.",
        "mission_acceptance_criteria": ["Preserve failure behavior."],
    }
    frozen = contract._materialize_contract(task)
    assert contract._is_frozen_contract(frozen) is True

    frozen["acceptance_criteria"] = ["Different criterion."]
    assert contract._is_frozen_contract(frozen) is False


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


def test_duplicate_rejection_precheck_reloads_live_contract() -> None:
    prior_calls: list[str] = []

    def prior_rejection(_task, fingerprint):
        prior_calls.append(fingerprint)
        return {"accepted": False, "fingerprint": fingerprint}

    epoch = SimpleNamespace()
    cw, _agent, _guarded, terminal = _install(
        task={"id": "code_prior_race", "prompt": "Do the work."},
        epoch=epoch,
        prior_rejection=prior_rejection,
    )
    before = cw.load_task("code_prior_race")
    stale_fingerprint = terminal.semantic_acceptance_fingerprint(
        before,
        diff_text="review-diff",
    )

    contract.set_acceptance_criteria(cw, "code_prior_race", ["New operator criterion."])

    assert terminal._prior_rejection(before, stale_fingerprint) == {}
    assert prior_calls == []


def test_grounding_failure_is_retryable_instead_of_crashing_finish() -> None:
    cw, _agent, guarded, _terminal = _install(
        task={
            "id": "code_grounding_error",
            "prompt": "Do the work.",
            "agent_cycle": 6,
        },
        # Missing mission-delta methods deliberately makes repository grounding
        # fail before the backend reviewer can run.
        epoch=SimpleNamespace(),
    )

    review = asyncio.run(
        guarded._semantic_acceptance_review(
            "code_grounding_error",
            cw.load_task("code_grounding_error"),
            diff_text="+ changed = True",
        )
    )

    assert review["accepted"] is False
    assert review["review_error"] is True
    assert "grounding failed" in review["reason"]
    assert review["fingerprint"]


def test_mission_acceptance_refuses_accepted_event_for_stale_fingerprint() -> None:
    state = {"review": {"accepted": True, "fingerprint": "stale"}, "calls": 0}

    def latest_accepted_review(_task):
        return dict(state["review"])

    def mission_review_diff(_cw, _agent, _task_id, _task):
        return "review-diff"

    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id, **_kwargs):
        state["calls"] += 1

    epoch = SimpleNamespace(
        KEY="coding_mission_acceptance_epoch",
        SCHEMA="nexus_coding_mission_acceptance_epoch.v1",
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    cw, agent, _guarded, terminal = _install(
        task={"id": "code_accept_race", "prompt": "Do the work.", "agent_cycle": 3},
        epoch=epoch,
    )

    epoch._record_semantic_acceptance(
        terminal, cw, agent, "code_accept_race",
        reviewed_fingerprint="stale", reviewed_cycle=3,
    )
    assert state["calls"] == 0

    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        cw.load_task("code_accept_race"),
        diff_text="review-diff",
    )
    epoch._record_semantic_acceptance(
        terminal, cw, agent, "code_accept_race",
        reviewed_fingerprint=state["review"]["fingerprint"], reviewed_cycle=3,
    )
    assert state["calls"] == 1


def test_unfrozen_migrated_review_missing_fingerprint_is_logged_and_blocked(caplog) -> None:
    state = {"calls": 0}

    def latest_accepted_review(_task):
        return {"accepted": True, "fingerprint": "", "cycle": 8}

    def mission_review_diff(_cw, _agent, _task_id, _task):
        return "review-diff"

    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id, **_kwargs):
        state["calls"] += 1

    epoch_obj = SimpleNamespace(
        KEY="coding_mission_acceptance_epoch",
        SCHEMA="nexus_coding_mission_acceptance_epoch.v1",
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    cw, agent, _guarded, terminal = _install(
        task={
            "id": "code_unfrozen_missing_review_fp",
            "prompt": "Do the work.",
            "agent_cycle": 8,
        },
        epoch=epoch_obj,
    )

    with caplog.at_level(logging.WARNING, logger=contract.__name__):
        epoch_obj._record_semantic_acceptance(
            terminal, cw, agent, "code_unfrozen_missing_review_fp"
        )

    assert state["calls"] == 0
    assert "no complete invocation-local review identity was supplied" in caplog.text


def test_frozen_contract_missing_review_fingerprint_is_logged_and_blocked(caplog) -> None:
    state = {"calls": 0}
    task = {"id": "code_missing_review_fp", "prompt": "Do the work.", "agent_cycle": 8}
    task[contract.KEY] = contract._materialize_contract(task)

    def latest_accepted_review(_task):
        return {"accepted": True, "fingerprint": ""}

    def mission_review_diff(_cw, _agent, _task_id, _task):
        return "review-diff"

    def record_semantic_acceptance(_terminal, _cw, _agent, _task_id, **_kwargs):
        state["calls"] += 1

    epoch = SimpleNamespace(
        KEY="coding_mission_acceptance_epoch",
        SCHEMA="nexus_coding_mission_acceptance_epoch.v1",
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    cw, agent, _guarded, terminal = _install(task=task, epoch=epoch)

    with caplog.at_level(logging.WARNING, logger=contract.__name__):
        epoch._record_semantic_acceptance(terminal, cw, agent, "code_missing_review_fp")

    assert state["calls"] == 0
    assert "no complete invocation-local review identity was supplied" in caplog.text


def test_mission_acceptance_clears_state_if_workspace_changes_during_record() -> None:
    state = {"review": {"accepted": True, "fingerprint": "", "cycle": 4}}

    def latest_accepted_review(_task):
        return dict(state["review"])

    def mission_review_diff(_cw, _agent, _task_id, task):
        return f"review-diff-v{int(task.get('repo_version') or 1)}"

    def record_semantic_acceptance(
        terminal,
        cw,
        _agent,
        task_id,
        *,
        reviewed_fingerprint="",
        reviewed_cycle=None,
        return_publication=False,
    ):
        before = cw.load_task(task_id)
        if not reviewed_fingerprint or reviewed_cycle is None:
            return {} if return_publication else False
        if int(before.get("agent_cycle") or 0) != int(reviewed_cycle):
            return {} if return_publication else False
        accepted_fp = terminal.semantic_acceptance_fingerprint(
            before,
            diff_text=mission_review_diff(cw, None, task_id, before),
        )
        if accepted_fp != reviewed_fingerprint:
            return {} if return_publication else False
        generation = int(
            dict(before.get("coding_mission_acceptance_epoch") or {}).get(
                "acceptance_publication_generation"
            )
            or 0
        ) + 1

        def apply(latest):
            latest["coding_mission_acceptance_epoch"] = {
                "schema": "nexus_coding_mission_acceptance_epoch.v1",
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "head-v1",
                "accepted_run_id": "run-v1",
                "accepted_fingerprint": accepted_fp,
                "accepted_diff_sha256": "diff-v1",
                "acceptance_publication_generation": generation,
            }
            # Simulate a repository/controller mutation landing after review but
            # before the legacy acceptance recorder returns.
            latest["repo_version"] = 2

        cw.mutate_task(task_id, apply)
        if return_publication:
            return {
                "fingerprint": accepted_fp,
                "publication_generation": generation,
            }
        return True

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
            "agent_cycle": 4,
        },
        epoch=epoch,
    )
    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        cw.load_task("code_accept_post_race"),
        diff_text="review-diff-v1",
    )

    epoch._record_semantic_acceptance(
        terminal, cw, agent, "code_accept_post_race",
        reviewed_fingerprint=state["review"]["fingerprint"], reviewed_cycle=4,
    )

    accepted = cw.task["coding_mission_acceptance_epoch"]
    assert cw.task["repo_version"] == 2
    assert accepted["status"] == "pending"
    assert accepted["accepted_at"] == 0.0
    assert accepted["accepted_head"] == ""
    assert accepted["accepted_run_id"] == ""
    assert accepted["accepted_fingerprint"] == ""
    assert accepted["accepted_diff_sha256"] == ""
    assert accepted["acceptance_publication_generation"] == 1


def test_stale_cleanup_does_not_erase_newer_concurrent_acceptance() -> None:
    state = {"review": {"accepted": True, "fingerprint": "", "cycle": 4}}
    epoch_key = "coding_mission_acceptance_epoch"
    epoch_schema = "nexus_coding_mission_acceptance_epoch.v1"

    class _RaceCW(_MemoryCW):
        def __init__(self, task: dict):
            super().__init__(task)
            self.mutation_count = 0
            self.fresh_fingerprint = ""

        def mutate_task(self, _task_id: str, apply):
            self.mutation_count += 1
            latest = dict(self.task)
            if self.mutation_count == 2:
                # A second finish publishes a fresh acceptance after the stale
                # recorder detected its mismatch but before cleanup acquires the lock.
                latest[epoch_key] = {
                    "schema": epoch_schema,
                    "status": "semantic_accepted",
                    "accepted_at": 2.0,
                    "accepted_head": "head-v2",
                    "accepted_run_id": "run-v2",
                    "accepted_fingerprint": self.fresh_fingerprint,
                    "accepted_diff_sha256": "diff-v2",
                    "acceptance_publication_generation": 2,
                }
            apply(latest)
            self.task = latest
            return dict(latest)

    def latest_accepted_review(_task):
        return dict(state["review"])

    def mission_review_diff(_cw, _agent, _task_id, task):
        return f"review-diff-v{int(task.get('repo_version') or 1)}"

    def record_semantic_acceptance(
        terminal,
        cw,
        _agent,
        task_id,
        *,
        reviewed_fingerprint="",
        reviewed_cycle=None,
        return_publication=False,
    ):
        before = cw.load_task(task_id)
        if not reviewed_fingerprint or reviewed_cycle is None:
            return {} if return_publication else False
        if int(before.get("agent_cycle") or 0) != int(reviewed_cycle):
            return {} if return_publication else False
        accepted_fp = terminal.semantic_acceptance_fingerprint(
            before,
            diff_text=mission_review_diff(cw, None, task_id, before),
        )
        if accepted_fp != reviewed_fingerprint:
            return {} if return_publication else False
        generation = int(
            dict(before.get(epoch_key) or {}).get("acceptance_publication_generation") or 0
        ) + 1

        def apply(latest):
            latest[epoch_key] = {
                "schema": epoch_schema,
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "head-v1",
                "accepted_run_id": "run-v1",
                "accepted_fingerprint": accepted_fp,
                "accepted_diff_sha256": "diff-v1",
                "acceptance_publication_generation": generation,
            }
            latest["repo_version"] = 2

        cw.mutate_task(task_id, apply)
        if return_publication:
            return {
                "fingerprint": accepted_fp,
                "publication_generation": generation,
            }
        return True

    epoch = SimpleNamespace(
        KEY=epoch_key,
        SCHEMA=epoch_schema,
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
    race_cw = _RaceCW(
        {
            "id": "code_accept_cleanup_race",
            "prompt": "Do the work.",
            "repo_version": 1,
            "agent_cycle": 4,
        }
    )
    cw, agent, _guarded, terminal = _install(
        task=race_cw.task,
        epoch=epoch,
        cw_override=race_cw,
    )
    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        cw.load_task("code_accept_cleanup_race"),
        diff_text="review-diff-v1",
    )
    v2_task = cw.load_task("code_accept_cleanup_race")
    v2_task["repo_version"] = 2
    race_cw.fresh_fingerprint = terminal.semantic_acceptance_fingerprint(
        v2_task,
        diff_text="review-diff-v2",
    )

    epoch._record_semantic_acceptance(
        terminal, cw, agent, "code_accept_cleanup_race",
        reviewed_fingerprint=state["review"]["fingerprint"], reviewed_cycle=4,
    )

    accepted = cw.task[epoch_key]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == race_cw.fresh_fingerprint
    assert accepted["accepted_head"] == "head-v2"
    assert accepted["accepted_diff_sha256"] == "diff-v2"
    assert accepted["acceptance_publication_generation"] == 2
