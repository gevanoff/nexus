from __future__ import annotations

from app import coding_runtime_guardrails as guards


def _observation(
    cycle: int,
    *,
    fingerprint: str = "unchanged",
    plan_revision: int = 2,
    validation_revision: int = 3,
    diff_review_revision: int = 4,
    finish_state: str = "running",
    guidance_revision: float = 10,
) -> guards.ProgressObservation:
    return guards.ProgressObservation(
        cycle=cycle,
        workspace_fingerprint=fingerprint,
        plan_revision=plan_revision,
        validation_revision=validation_revision,
        diff_review_revision=diff_review_revision,
        finish_state=finish_state,
        guidance_revision=guidance_revision,
    )


def test_restart_discards_exhausted_streak_but_counts_first_unchanged_cycle() -> None:
    exhausted = guards.ProgressState(
        observation=_observation(8),
        stagnant_cycles=8,
    )

    decision = guards.evaluate_cycle_progress(
        exhausted,
        _observation(1),
        max_stagnant_cycles=8,
    )

    assert decision.progressed is False
    assert decision.pause is False
    assert decision.state.stagnant_cycles == 1
    assert decision.state.observation.validation_revision == 3
    assert decision.state.observation.diff_review_revision == 4


def test_restarted_run_receives_a_full_new_no_progress_budget() -> None:
    state = guards.ProgressState(
        observation=_observation(8),
        stagnant_cycles=8,
    )

    for cycle in range(1, 8):
        decision = guards.evaluate_cycle_progress(
            state,
            _observation(cycle),
            max_stagnant_cycles=8,
        )
        assert decision.pause is False
        assert decision.state.stagnant_cycles == cycle
        state = decision.state

    decision = guards.evaluate_cycle_progress(
        state,
        _observation(8),
        max_stagnant_cycles=8,
    )
    assert decision.pause is True
    assert decision.state.stagnant_cycles == 8
    assert decision.reason_code == "no_progress_limit"


def test_first_continuation_cycle_with_real_progress_resets_to_zero() -> None:
    exhausted = guards.ProgressState(
        observation=_observation(8),
        stagnant_cycles=8,
    )

    decision = guards.evaluate_cycle_progress(
        exhausted,
        _observation(1, fingerprint="changed"),
        max_stagnant_cycles=8,
    )

    assert decision.progressed is True
    assert decision.pause is False
    assert decision.state.stagnant_cycles == 0


def test_same_run_context_compaction_does_not_reset_streak() -> None:
    state = guards.ProgressState(
        observation=_observation(4),
        stagnant_cycles=4,
    )

    decision = guards.evaluate_cycle_progress(
        state,
        _observation(5),
        max_stagnant_cycles=8,
    )

    assert decision.progressed is False
    assert decision.pause is False
    assert decision.state.stagnant_cycles == 5
