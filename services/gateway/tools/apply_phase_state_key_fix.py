from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one target, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


resilience = ROOT / "services/gateway/app/coding_stagnation_resilience.py"
replace_once(
    resilience,
    '''        "diff_review_revision": as_int(observation.get("diff_review_revision")),
        "finish_state": str(observation.get("finish_state") or "running"),
    }
''',
    '''        "diff_review_revision": as_int(observation.get("diff_review_revision")),
        "finish_state": str(observation.get("finish_state") or "running"),
        "evidence_fingerprint": str(observation.get("evidence_fingerprint") or ""),
        "work_phase": str(observation.get("work_phase") or "discovery"),
    }
''',
)
replace_once(
    resilience,
    '''    if previous_state_key != state_key or progress_stagnant_cycles <= 0:
        cycles = 0
''',
    '''    if previous_state_key != state_key:
        cycles = max(0, progress_stagnant_cycles)
''',
)

tests = ROOT / "services/gateway/tests/test_coding_work_phases.py"
text = tests.read_text(encoding="utf-8")
addition = '''\n\ndef test_phase_and_validation_evidence_change_durable_state_key():
    task = _review_task()
    task["agent_progress_state"] = {
        "observation": {
            "workspace_fingerprint": "same",
            "validation_revision": 0,
            "diff_review_revision": 0,
            "finish_state": "running",
            "evidence_fingerprint": "validation-a",
            "work_phase": "discovery",
        }
    }
    initial = resilience.durable_state_key(task)

    task["agent_progress_state"]["observation"]["evidence_fingerprint"] = "validation-b"
    after_evidence = resilience.durable_state_key(task)
    assert after_evidence != initial

    task["agent_progress_state"]["observation"]["work_phase"] = "decision"
    assert resilience.durable_state_key(task) != after_evidence
'''
if "test_phase_and_validation_evidence_change_durable_state_key" in text:
    raise RuntimeError("durable-state phase test already exists")
tests.write_text(text.rstrip() + addition + "\n", encoding="utf-8")

print("Applied phase state-key reset fix.")
