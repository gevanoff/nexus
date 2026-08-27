from __future__ import annotations

import sys
from types import SimpleNamespace

import app
from app import coding_acceptance_convergence_hardening as convergence
from app import coding_forced_action
from app import coding_pr93_review_hardening as review
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


class MissionEpoch:
    KEY = "coding_mission_acceptance_epoch"
    REFUTATION_TOOL = "coding_refute_hypothesis"


def test_structured_parser_preserves_multiline_locator_evidence_and_strips_suffix():
    note = (
        "Root cause: config discovery aborts too early\n"
        "Repository evidence: two entries\n"
        "app.py: reads DEFAULT_BACKEND at import\n"
        "config.py: writes it from env\n"
        "Competing explanation checked: frontend rendering already consumes management metadata\n"
        "Expected result: management metadata survives catalog failure\n"
        "Status (auto): waiting\n"
        "状態: 実行待ち"
    )
    fields = review.structured_hypothesis_fields(note)
    assert fields["Repository evidence"] == (
        "two entries\n"
        "app.py: reads DEFAULT_BACKEND at import\n"
        "config.py: writes it from env"
    )
    assert fields["Expected result"] == "management metadata survives catalog failure"


def test_structured_fingerprint_v2_is_stable_across_trailing_bookkeeping():
    base = (
        "Root cause: typo\n"
        "Repository evidence: app.py:10\n"
        "Competing explanation checked: cache\n"
        "Expected result: ok"
    )
    with_status = base + "\nStatus (auto): waiting\n状態: 実行待ち"
    assert review.structured_hypothesis_fingerprint_from_note(base) == (
        review.structured_hypothesis_fingerprint_from_note(with_status)
    )


def test_atomic_validation_persistence_ignores_nonvalidation_after_success(monkeypatch):
    original = terminal._persist_validation_provenance
    monkeypatch.setattr(terminal, "_persist_validation_provenance", original)
    monkeypatch.setattr(
        terminal,
        "_coding_pr93_atomic_validation_persistence_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        terminal,
        "_coding_validation_history_continuity_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        terminal,
        "_coding_pr93_validation_persistence_fixed",
        False,
        raising=False,
    )
    task = {
        "id": "code-history-v2",
        "agent_run_id": "run-1",
        "agent_cycle": 1,
        "commands": [
            {"label": "agent-command", "argv": ["pytest", "-q"], "ok": True, "ts": 11.0},
        ],
    }
    cw = CW(task)
    review._install_atomic_validation_persistence(convergence)
    fixed = terminal._persist_validation_provenance
    terminal._persist_validation_provenance(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["pytest", "-q"],
        cwd="",
        result={"ok": True},
    )
    before = dict(task[review._VALIDATION_KEY])
    before_history = list(before["history"])
    task["commands"].append(
        {"label": "agent-command", "argv": ["cat", "missing"], "ok": False, "ts": 12.0}
    )
    terminal._persist_validation_provenance(
        cw,
        coding_work_phases,
        task_id=task["id"],
        argv=["cat", "missing"],
        cwd="",
        result={"ok": False, "stderr": "missing"},
    )
    assert terminal._persist_validation_provenance is fixed
    assert task[review._VALIDATION_KEY]["argv"] == ["pytest", "-q"]
    assert task[review._VALIDATION_KEY]["history"] == before_history
    assert terminal._coding_validation_history_continuity_installed is True
    assert terminal._coding_pr93_validation_persistence_fixed is True


def test_acceptance_validation_installer_is_neutered_before_use(monkeypatch):
    fake_convergence = SimpleNamespace(
        _validation_result_missing_tool=lambda _result: False,
        _validation_records_from_history=lambda _task, _threshold: [],
        _validation_records_from_events=lambda _task, _threshold: [],
        _validation_records_from_commands=lambda _task, _threshold: [],
        _terminal_state=lambda _cw, _mission, _task: {},
        _semantic_rejection_guard_blocks=lambda *_args: False,
    )
    fake_resume = SimpleNamespace(
        post_edit_state=lambda *_args: {},
        _install_sentinel_failed_resume_guard=lambda: None,
        _install_convergence_review_fixes=lambda *_args: None,
        _install_validation_persistence_fix=lambda *_args: None,
        _install_validation_side_effect_restamp=lambda *_args: None,
        _restamp_validation_after_workspace_mutation=lambda *_args: None,
        _unresolved_validation_failures=lambda *_args: [],
    )
    monkeypatch.setattr(
        terminal,
        "_coding_pr93_atomic_validation_persistence_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        terminal,
        "_coding_validation_history_continuity_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        terminal,
        "_coding_pr93_validation_persistence_fixed",
        False,
        raising=False,
    )
    review.preinstall(CW({}), MissionEpoch, fake_convergence, fake_resume)
    fixed = terminal._persist_validation_provenance
    fake_convergence._install_validation_continuity()
    fake_resume._install_validation_persistence_fix(fake_convergence)
    fake_convergence._install_validation_continuity()
    assert terminal._persist_validation_provenance is fixed


def test_validation_side_effect_restamp_is_disabled(monkeypatch):
    fake_resume = SimpleNamespace(
        post_edit_state=lambda *_args: {},
        _install_convergence_review_fixes=lambda *_args: None,
        _install_validation_persistence_fix=lambda *_args: None,
        _install_validation_side_effect_restamp=lambda *_args: (_ for _ in ()).throw(
            AssertionError("legacy restamp installed")
        ),
        _restamp_validation_after_workspace_mutation=lambda *_args: (_ for _ in ()).throw(
            AssertionError("legacy restamp executed")
        ),
        _unresolved_validation_failures=lambda *_args: [],
    )
    review._install_resume_bridges(fake_resume, convergence, MissionEpoch)
    fake_resume._install_validation_side_effect_restamp(object(), object(), object())
    assert fake_resume._restamp_validation_after_workspace_mutation(None, None, "id", ["pytest"]) is None


def test_sentinel_guard_handles_immutable_policy_without_boot_failure(monkeypatch):
    fake_sentinel = SimpleNamespace(_CODING_AUTO_RESUME_BLOCKERS=frozenset({"finish_gate"}))
    monkeypatch.setitem(sys.modules, "app.sentinel_runtime", fake_sentinel)
    monkeypatch.setattr(app, "sentinel_runtime", fake_sentinel, raising=False)
    fake_resume = SimpleNamespace()
    review._install_safe_sentinel_guard(fake_resume)
    fake_resume._install_sentinel_failed_resume_guard()
    assert fake_sentinel._CODING_AUTO_RESUME_BLOCKERS == {"finish_gate", "run_failed"}


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


def test_repair_state_explicitly_advertises_refutation():
    fake_resume = SimpleNamespace(
        post_edit_state=lambda *_args: {
            "validation_repair": True,
            "allowed_tools": ["coding_run_command", "coding_replace_text", "coding_finish"],
            "required_action": "repair the failing validation.",
        },
        _install_convergence_review_fixes=lambda *_args: None,
        _install_validation_persistence_fix=lambda *_args: None,
        _install_validation_side_effect_restamp=lambda *_args: None,
        _restamp_validation_after_workspace_mutation=lambda *_args: None,
        _unresolved_validation_failures=lambda *_args: [],
    )
    review._install_resume_bridges(fake_resume, convergence, MissionEpoch)
    state = fake_resume.post_edit_state(None, MissionEpoch, convergence, {})
    assert "coding_refute_hypothesis" in state["allowed_tools"]
    assert "explicitly available" in state["required_action"]
