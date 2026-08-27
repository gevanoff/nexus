"""Regressions from the PR #93 reviews, pinned against the in-place implementations.

These previously lived in test_coding_pr93_review_hardening.py against a
separate patch module; the fixes now live directly in
coding_acceptance_convergence_hardening (parser, fingerprints, validation
persistence, terminal-state rejection guard) and
coding_resume_convergence_hardening (repair state, restamp gate, Sentinel
verification).
"""

from __future__ import annotations

from app import coding_acceptance_convergence_hardening as convergence
from app import coding_forced_action
from app import coding_terminal_acceptance_hardening as terminal
from app import coding_work_phases


class CW:
    def __init__(self, task):
        self.task = task

    def load_task(self, _task_id):
        return self.task

    def save_task(self, task):
        self.task = task
        return task

    def mutate_task(self, _task_id, mutator):
        mutator(self.task)
        return self.task


MULTILINE_NOTE = (
    "Root cause: config loader ignores the env override\n"
    "Repository evidence: two entries\n"
    "app.py: reads DEFAULT_BACKEND at import\n"
    "config.py: writes it from the environment\n"
    "Competing explanation checked: stale bytecode cache ruled out\n"
    "Expected result: override honored\n"
    "settings.py: exports the resolved value"
)


def test_structured_parser_preserves_multiline_locator_evidence():
    fields = convergence._structured_hypothesis_fields(MULTILINE_NOTE)
    assert fields["Repository evidence"] == (
        "two entries\n"
        "app.py: reads DEFAULT_BACKEND at import\n"
        "config.py: writes it from the environment"
    )
    # Locator-style lines after the final field are content, not bookkeeping.
    assert fields["Expected result"] == (
        "override honored\nsettings.py: exports the resolved value"
    )


def test_structured_parser_strips_trailing_bookkeeping_suffix():
    note = MULTILINE_NOTE + "\nStatus (auto): waiting on review\nNext step: rerun"
    fields = convergence._structured_hypothesis_fields(note)
    assert fields["Expected result"] == (
        "override honored\nsettings.py: exports the resolved value"
    )


def test_structured_fingerprint_is_stable_across_trailing_bookkeeping():
    with_status = MULTILINE_NOTE + "\nStatus: retry 3"
    assert convergence._structured_hypothesis_fingerprint_from_note(
        MULTILINE_NOTE
    ) == convergence._structured_hypothesis_fingerprint_from_note(with_status)


def test_structured_fingerprint_differs_for_material_hypothesis_change():
    changed = MULTILINE_NOTE.replace("override honored", "override rejected loudly")
    assert convergence._structured_hypothesis_fingerprint_from_note(
        MULTILINE_NOTE
    ) != convergence._structured_hypothesis_fingerprint_from_note(changed)


def test_material_hypothesis_tolerates_fingerprints_from_prior_parser_versions():
    """A parser/algorithm change must never manufacture a material update."""
    task = {
        "project_plan": {"revision": 3, "updated_at": 30.0, "note": MULTILINE_NOTE},
        "agent_hypothesis_lifecycle": {
            "status": "consumed",
            "plan_revision": 2,
            # Fingerprint recorded under an older parser revision: unknown hash,
            # but the consumed note itself is durable and recomputes to match.
            "structured_hypothesis_fingerprint": "legacy-algorithm-hash",
            "consumed_hypothesis_note": MULTILINE_NOTE,
        },
    }
    assert convergence._material_hypothesis_updated_at(task) == 0.0


def _install_atomic_persistence():
    convergence._install_validation_continuity()
    return terminal._persist_validation_provenance


def test_atomic_validation_persistence_ignores_nonvalidation_after_success():
    persist = _install_atomic_persistence()
    task = {
        "id": "code-history",
        "agent_run_id": "run-1",
        "agent_cycle": 1,
        "commands": [
            {"label": "agent-command", "argv": ["pytest", "-q"], "ok": True, "ts": 11.0},
        ],
    }
    cw = CW(task)
    persist(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["pytest", "-q"],
        cwd="",
        result={"ok": True},
    )
    durable_before = dict(task[convergence._VALIDATION_KEY])
    persist(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["cat", "missing-file"],
        cwd="",
        result={"ok": False, "stderr": "cat: missing-file: No such file"},
    )
    assert task[convergence._VALIDATION_KEY] == durable_before
    assert convergence._validation_obligations_ready(task, 10.0) is True


def test_validation_continuity_installer_is_idempotent():
    first = _install_atomic_persistence()
    convergence._install_validation_continuity()
    assert terminal._persist_validation_provenance is first


def test_repair_mode_run_command_is_runtime_validation_only(monkeypatch):
    state = {
        "schema": "nexus_coding_resume_convergence.v1",
        "status": "active",
        "action_kind": "edit",
        "allowed_tools": ["coding_run_command", "coding_replace_text", "coding_finish"],
        "required_action": "repair validation failure",
        "state_key": "repair",
        "stage": "post_edit_validation_repair",
    }
    monkeypatch.setattr(coding_forced_action, "active_state", lambda _task: dict(state))
    allowed, rejected = coding_forced_action.evaluate_tool_call(
        {},
        name="coding_run_command",
        args={"argv": ["sed", "-i", "s/a/b/", "app.py"]},
        is_validation_command=coding_work_phases.is_validation_command,
    )
    assert allowed is False
    assert rejected["error"] == "forced_action_tool_rejected"
    allowed, _ = coding_forced_action.evaluate_tool_call(
        {},
        name="coding_run_command",
        args={"argv": ["pytest", "-q"]},
        is_validation_command=coding_work_phases.is_validation_command,
    )
    assert allowed is True
