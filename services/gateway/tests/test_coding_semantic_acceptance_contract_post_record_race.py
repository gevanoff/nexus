from __future__ import annotations

from types import SimpleNamespace

from app import coding_semantic_acceptance
from app import coding_semantic_acceptance_contract as contract


EPOCH_KEY = "coding_mission_acceptance_epoch"
EPOCH_SCHEMA = "nexus_coding_mission_acceptance_epoch.v1"


class _RaceCW:
    def __init__(self, task: dict):
        self.task = dict(task)
        self.publish_fresh_on_next_load = False
        self.fresh_fingerprint = ""

    def load_task(self, _task_id: str) -> dict:
        if self.publish_fresh_on_next_load:
            self.publish_fresh_on_next_load = False
            self.task[EPOCH_KEY] = {
                "schema": EPOCH_SCHEMA,
                "status": "semantic_accepted",
                "accepted_at": 2.0,
                "accepted_head": "head-v2",
                "accepted_run_id": "run-v2",
                "accepted_fingerprint": self.fresh_fingerprint,
                "accepted_diff_sha256": "diff-v2",
                "acceptance_publication_generation": 2,
            }
        return dict(self.task)

    def save_task(self, task: dict) -> dict:
        self.task = dict(task)
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        latest = dict(self.task)
        apply(latest)
        self.task = latest
        return dict(latest)


def test_stale_recorder_does_not_target_acceptance_published_before_post_record_load() -> None:
    state = {"review": {"accepted": True, "fingerprint": ""}}
    cw = _RaceCW(
        {
            "id": "code_accept_post_record_load_race",
            "prompt": "Do the work.",
            "repo_version": 1,
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
        return_publication=False,
    ):
        before = cw_obj.load_task(task_id)
        stale_fingerprint = terminal_obj.semantic_acceptance_fingerprint(
            before,
            diff_text=mission_review_diff(cw_obj, None, task_id, before),
        )
        generation = int(
            dict(before.get(EPOCH_KEY) or {}).get("acceptance_publication_generation") or 0
        ) + 1

        def apply(latest):
            latest[EPOCH_KEY] = {
                "schema": EPOCH_SCHEMA,
                "status": "semantic_accepted",
                "accepted_at": 1.0,
                "accepted_head": "head-v1",
                "accepted_run_id": "run-v1",
                "accepted_fingerprint": stale_fingerprint,
                "accepted_diff_sha256": "diff-v1",
                "acceptance_publication_generation": generation,
            }
            latest["repo_version"] = 2

        cw_obj.mutate_task(task_id, apply)
        # The competing finish publishes after this recorder returns but before
        # the wrapper's post-record load samples shared task state.
        cw_obj.publish_fresh_on_next_load = True
        if return_publication:
            return {
                "fingerprint": stale_fingerprint,
                "publication_generation": generation,
            }
        return True

    epoch = SimpleNamespace(
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
        epoch,
        terminal,
        coding_semantic_acceptance,
        debug,
    )

    initial = cw.load_task("code_accept_post_record_load_race")
    state["review"]["fingerprint"] = terminal.semantic_acceptance_fingerprint(
        initial,
        diff_text="review-diff-v1",
    )
    v2_task = dict(initial)
    v2_task["repo_version"] = 2
    cw.fresh_fingerprint = terminal.semantic_acceptance_fingerprint(
        v2_task,
        diff_text="review-diff-v2",
    )

    epoch._record_semantic_acceptance(
        terminal,
        cw,
        agent,
        "code_accept_post_record_load_race",
    )

    accepted = cw.task[EPOCH_KEY]
    assert accepted["status"] == "semantic_accepted"
    assert accepted["accepted_fingerprint"] == cw.fresh_fingerprint
    assert accepted["accepted_head"] == "head-v2"
    assert accepted["accepted_diff_sha256"] == "diff-v2"
    assert accepted["acceptance_publication_generation"] == 2
