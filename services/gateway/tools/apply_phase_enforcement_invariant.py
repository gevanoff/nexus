from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def replace_once(relative: str, old: str, new: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{relative}: expected one target, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "services/gateway/app/coding_runtime_guardrails.py",
    '''            current.finish_state != prior.finish_state,\n            current.work_phase != prior.work_phase,\n            (\n''',
    '''            current.finish_state != prior.finish_state,\n            (\n''',
)

replace_once(
    "services/gateway/app/coding_stagnation_resilience.py",
    '''        "evidence_fingerprint": str(observation.get("evidence_fingerprint") or ""),\n        "work_phase": str(observation.get("work_phase") or "discovery"),\n''',
    '''        "evidence_fingerprint": str(observation.get("evidence_fingerprint") or ""),\n''',
)

replace_once(
    "services/gateway/tests/test_coding_work_phases.py",
    '''def test_phase_and_validation_evidence_change_durable_state_key():\n''',
    '''def test_validation_evidence_changes_durable_state_key_but_phase_does_not():\n''',
)

replace_once(
    "services/gateway/tests/test_coding_work_phases.py",
    '''    task["agent_progress_state"]["observation"]["work_phase"] = "decision"\n    assert resilience.durable_state_key(task) != after_evidence\n''',
    '''    task["agent_progress_state"]["observation"]["work_phase"] = "decision"\n    assert resilience.durable_state_key(task) == after_evidence\n\n\ndef test_phase_transition_does_not_mint_guardrail_progress():\n    previous = guardrails.ProgressState(\n        observation=guardrails.ProgressObservation(\n            cycle=5,\n            workspace_fingerprint="same",\n            plan_revision=0,\n            validation_revision=1,\n            diff_review_revision=0,\n            finish_state="running",\n            guidance_revision=0,\n            evidence_fingerprint="validation-a",\n            work_phase="discovery",\n        ),\n        stagnant_cycles=4,\n    )\n    current = guardrails.ProgressObservation(\n        cycle=6,\n        workspace_fingerprint="same",\n        plan_revision=0,\n        validation_revision=1,\n        diff_review_revision=0,\n        finish_state="running",\n        guidance_revision=0,\n        evidence_fingerprint="validation-a",\n        work_phase="decision",\n    )\n\n    decision = guardrails.evaluate_cycle_progress(previous, current, max_stagnant_cycles=8)\n\n    assert decision.progressed is False\n    assert decision.state.stagnant_cycles == 5\n''',
)

print("Applied phase enforcement invariant.")
