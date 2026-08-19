from __future__ import annotations

from types import SimpleNamespace

from app import coding_agent
from app import coding_edit_evidence_continuity as continuity


STATE = {
    "action_kind": "edit",
    "evidence_provenance_enforced": True,
    "hypothesis_causal_evidence_linked": True,
    "causal_evidence_targets": ["services/gateway/app/ui_routes.py"],
    "activated_at": 10.0,
    "durable_hypothesis_note_updated_at": 20.0,
}
AGENT = SimpleNamespace(
    _tool_result_modified_workspace=coding_agent._tool_result_modified_workspace,
)


def _events(name: str, args: dict, result: dict) -> dict:
    return {
        "agent_events": [
            {
                "type": "tool_started",
                "cycle": 11,
                "tool_call_id": "call-1",
                "name": name,
                "args": dict(args),
                "ts": 30.0,
            },
            {
                "type": "tool_finished",
                "cycle": 11,
                "tool_call_id": "call-1",
                "name": name,
                "result": dict(result),
                "ts": 31.0,
            },
        ]
    }


def test_check_only_patch_does_not_end_edit_evidence_replay():
    task = _events(
        "coding_apply_patch",
        {"patch": "diff --git a/a.py b/a.py", "check_only": True},
        {"ok": True, "check_only": True},
    )

    assert continuity._successful_edit_after_authorization(AGENT, task, STATE) is False
    assert continuity._edit_replay_required(AGENT, task, STATE) is True


def test_zero_replacement_edit_does_not_end_edit_evidence_replay():
    task = _events(
        "coding_replace_text",
        {"path": "a.py", "old_text": "old", "new_text": "new"},
        {"ok": True, "replacements": 0},
    )

    assert continuity._successful_edit_after_authorization(AGENT, task, STATE) is False
    assert continuity._edit_replay_required(AGENT, task, STATE) is True


def test_real_patch_mutation_ends_edit_evidence_replay():
    task = _events(
        "coding_apply_patch",
        {"patch": "diff --git a/a.py b/a.py", "check_only": False},
        {"ok": True, "apply": {"ok": True}},
    )

    assert continuity._successful_edit_after_authorization(AGENT, task, STATE) is True
    assert continuity._edit_replay_required(AGENT, task, STATE) is False
