from __future__ import annotations

from types import SimpleNamespace

from app import coding_mission_acceptance_epoch as epoch
from app import coding_semantic_acceptance
from app import coding_semantic_acceptance_contract as contract


class _OwnerRaceCW:
    def __init__(self, task: dict):
        self.task = dict(task)
        self.mutations = 0

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        self.mutations += 1
        latest = dict(self.task)
        if self.mutations == 1:
            # Stale recorder A wins B's first CAS with publication X/gen1.
            latest[epoch.KEY] = {
                **dict(latest[epoch.KEY]),
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "head-x1",
                "accepted_run_id": "run-x1",
                "accepted_fingerprint": "fp:x",
                "accepted_diff_sha256": "sha-x1",
                "acceptance_publication_generation": 1,
            }
        elif self.mutations == 2:
            # Between B's retry reload and final CAS, X is cleared and a new,
            # legitimate same-fingerprint publication X/gen2 appears. A
            # fingerprint-only CAS cannot distinguish this ABA transition.
            latest[epoch.KEY] = {
                **dict(latest[epoch.KEY]),
                "status": "semantic_accepted",
                "accepted_at": 2.0,
                "accepted_head": "head-x2",
                "accepted_run_id": "run-x2",
                "accepted_fingerprint": "fp:x",
                "accepted_diff_sha256": "sha-x2",
                "acceptance_publication_generation": 2,
            }
        apply(latest)
        self.task = latest
        return dict(latest)


def _owner_task() -> dict:
    return {
        "id": "code_generation_aba",
        "agent_cycle": 5,
        "agent_run_id": "run-y",
        "agent_events": [
            {
                "type": "semantic_acceptance_review",
                "cycle": 5,
                "accepted": True,
                "fingerprint": "fp:y",
            }
        ],
        epoch.KEY: {
            "schema": epoch.SCHEMA,
            "status": "pending",
            "base_head": "base",
            "last_mutation_at": 1.0,
            "last_mutation_run_id": "run-before",
            "accepted_fingerprint": "",
            "acceptance_publication_generation": 0,
        },
    }


def test_final_cas_does_not_overwrite_same_fingerprint_new_publication(monkeypatch) -> None:
    monkeypatch.setattr(
        epoch,
        "mission_delta_state",
        lambda _cw, _task_id, _task: {
            "ok": True,
            "has_delta": True,
            "base_head": "base",
            "current_head": "head-y",
            "diff_sha256": "sha-y",
        },
    )
    monkeypatch.setattr(
        epoch,
        "mission_review_diff",
        lambda _cw, _agent, _task_id, _task: "y",
    )
    cw = _OwnerRaceCW(_owner_task())
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"fp:{diff_text}"
    )

    published = epoch._record_semantic_acceptance(
        terminal,
        cw,
        SimpleNamespace(),
        cw.task["id"],
        reviewed_fingerprint="fp:y",
        reviewed_cycle=5,
    )

    assert published is False
    assert cw.mutations == 2
    accepted = cw.task[epoch.KEY]
    assert accepted["accepted_fingerprint"] == "fp:x"
    assert accepted["accepted_head"] == "head-x2"
    assert accepted["accepted_run_id"] == "run-x2"
    assert accepted["accepted_diff_sha256"] == "sha-x2"
    assert accepted["acceptance_publication_generation"] == 2


EPOCH_KEY = "coding_mission_acceptance_epoch"
EPOCH_SCHEMA = "nexus_coding_mission_acceptance_epoch.v1"


class _CleanupRaceCW:
    def __init__(self, task: dict):
        self.task = dict(task)
        self.mutations = 0
        self.reviewed_fingerprint = ""

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def save_task(self, task: dict) -> dict:
        self.task = dict(task)
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        self.mutations += 1
        latest = dict(self.task)
        if self.mutations == 2:
            # A newer finish republishes the same semantic fingerprint with a
            # distinct publication generation just before stale cleanup locks.
            latest[EPOCH_KEY] = {
                **dict(latest[EPOCH_KEY]),
                "status": "semantic_accepted",
                "accepted_at": 2.0,
                "accepted_head": "head-fresh",
                "accepted_run_id": "run-fresh",
                "accepted_fingerprint": self.reviewed_fingerprint,
                "accepted_diff_sha256": "sha-fresh",
                "acceptance_publication_generation": 2,
            }
        apply(latest)
        self.task = latest
        return dict(latest)


def test_stale_cleanup_preserves_new_same_fingerprint_publication() -> None:
    state = {"review": {"accepted": True, "fingerprint": "", "cycle": 5}}
    cw = _CleanupRaceCW(
        {
            "id": "code_cleanup_generation_aba",
            "prompt": "Do the work.",
            "repo_version": 1,
            "agent_cycle": 5,
            EPOCH_KEY: {
                "schema": EPOCH_SCHEMA,
                "status": "pending",
                "base_head": "base",
                "acceptance_publication_generation": 0,
            },
        }
    )
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

    def latest_accepted_review(_task):
        return dict(state["review"])

    def mission_review_diff(_cw, _agent, _task_id, task):
        return f"review-diff-v{int(task.get('repo_version') or 1)}"

    def record_semantic_acceptance(
        terminal_obj,
        cw_obj,
        _agent,
        task_id,
        *,
        reviewed_fingerprint="",
        reviewed_cycle=None,
        return_publication=False,
    ):
        before = cw_obj.load_task(task_id)
        if not reviewed_fingerprint or reviewed_cycle is None:
            return {} if return_publication else False
        if int(before.get("agent_cycle") or 0) != int(reviewed_cycle):
            return {} if return_publication else False
        fingerprint = terminal_obj.semantic_acceptance_fingerprint(
            before,
            diff_text=mission_review_diff(cw_obj, None, task_id, before),
        )
        if fingerprint != reviewed_fingerprint:
            return {} if return_publication else False

        def apply(latest):
            latest[EPOCH_KEY] = {
                **dict(latest[EPOCH_KEY]),
                "schema": EPOCH_SCHEMA,
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "head-stale",
                "accepted_run_id": "run-stale",
                "accepted_fingerprint": fingerprint,
                "accepted_diff_sha256": "sha-stale",
                "acceptance_publication_generation": 1,
            }
            # Make the just-published acceptance stale so the wrapper will
            # attempt cleanup after the owner recorder returns.
            latest["repo_version"] = 2

        cw_obj.mutate_task(task_id, apply)
        if return_publication:
            return {
                "fingerprint": fingerprint,
                "publication_generation": 1,
            }
        return True

    epoch_stub = SimpleNamespace(
        KEY=EPOCH_KEY,
        SCHEMA=EPOCH_SCHEMA,
        _latest_accepted_review=latest_accepted_review,
        mission_review_diff=mission_review_diff,
        _record_semantic_acceptance=record_semantic_acceptance,
    )
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
        epoch_stub,
        terminal,
        coding_semantic_acceptance,
        debug,
    )

    initial = cw.load_task("code_cleanup_generation_aba")
    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        initial,
        diff_text="review-diff-v1",
    )
    cw.reviewed_fingerprint = state["review"]["fingerprint"]

    epoch_stub._record_semantic_acceptance(
        terminal,
        cw,
        agent,
        "code_cleanup_generation_aba",
        reviewed_fingerprint=state["review"]["fingerprint"],
        reviewed_cycle=5,
    )

    accepted = cw.task[EPOCH_KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == cw.reviewed_fingerprint
    assert accepted["accepted_head"] == "head-fresh"
    assert accepted["accepted_run_id"] == "run-fresh"
    assert accepted["accepted_diff_sha256"] == "sha-fresh"
    assert accepted["acceptance_publication_generation"] == 2
