from __future__ import annotations

from types import SimpleNamespace

from app import coding_edit_evidence_continuity as continuity


class DebugReport:
    _coding_effective_policy_debug_installed = False

    @staticmethod
    def _sanitize(value):
        return value

    @staticmethod
    def collect_debug_snapshot(task_id, *, active_runner=None):
        return {
            "controller": {
                "forced_action": {
                    "action_kind": "evidence",
                    "canonical_action_kind": "edit",
                    "allowed_tools": ["coding_finish", "coding_update_plan"],
                },
                "forced_action_persisted": {"action_kind": "evidence"},
            }
        }


class CW:
    @staticmethod
    def load_task(task_id):
        return {
            "id": task_id,
            "agent_verified_evidence_replay": {
                "phase": "edit",
                "paths": ["services/gateway/app/ui_routes.py"],
                "chars": 3000,
            },
        }


class Forced:
    @staticmethod
    def active_state(task):
        return {
            "action_kind": "edit",
            "canonical_action_kind": "edit",
            "allowed_tools": [
                "coding_apply_patch",
                "coding_finish",
                "coding_replace_text",
                "coding_write_file",
            ],
            "hypothesis_ready": True,
            "hypothesis_causal_evidence_linked": True,
        }


def test_debug_report_uses_effective_policy_and_preserves_base_view():
    debug_report = DebugReport()
    agent = SimpleNamespace(forced_action=Forced())
    continuity._install_debug_effective_policy(agent, debug_report, CW())

    snapshot = debug_report.collect_debug_snapshot("code-1", active_runner=False)
    controller = snapshot["controller"]

    assert controller["forced_action"]["action_kind"] == "edit"
    assert controller["forced_action_effective"]["action_kind"] == "edit"
    assert controller["forced_action_base"]["action_kind"] == "evidence"
    assert controller["forced_action_persisted"]["action_kind"] == "evidence"
    assert controller["verified_evidence_replay"] == {
        "phase": "edit",
        "paths": ["services/gateway/app/ui_routes.py"],
        "chars": 3000,
    }
