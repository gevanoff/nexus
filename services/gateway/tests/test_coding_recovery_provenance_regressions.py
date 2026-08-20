from __future__ import annotations

from types import SimpleNamespace

from app import coding_evidence_policy as provenance
from app import coding_evidence_range_provenance as range_provenance
from app import coding_failed_edit_recovery as recovery
from app import coding_forced_action as forced


TARGET = "services/gateway/app/ui_routes.py"


def _range_state() -> dict:
    return {
        "requires_hypothesis": True,
        "action_kind": "edit",
        "canonical_action_kind": "edit",
        "canonical_required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        "activation_plan_revision": 0,
        "activation_event_count": 0,
        "activated_at": 10.0,
        "run_id": "run-1",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
        "hypothesis_causal_evidence_linked": True,
        "evidence_provenance_enforced": True,
    }


def _hypothesis(repository_evidence: str) -> str:
    return (
        "Root cause: backend management metadata is attached in the wrong control-flow location.\n"
        f"Repository evidence: {repository_evidence}\n"
        "Competing explanation checked: frontend rendering still consumes management.ui_url.\n"
        "Expected result: management navigation survives model-catalog failure."
    )


def _ranged_task(*, goal_evidence: str, note_evidence: str) -> dict:
    return {
        "agent_events": [
            {"ts": 1.0, "type": "started", "run_id": "run-1", "backend": "local_mlx"},
            {
                "ts": 2.0,
                "type": "tool_started",
                "tool_call_id": "read-1",
                "name": "coding_read_file_lines",
                "args": {"path": TARGET, "start_line": 1538, "line_count": 20},
            },
            {
                "ts": 3.0,
                "type": "tool_finished",
                "tool_call_id": "read-1",
                "name": "coding_read_file_lines",
                "result": {
                    "ok": True,
                    "path": TARGET,
                    "content": "bounded implementation",
                    "start_line": 1538,
                    "end_line": 1557,
                    "line_count": 20,
                    "total_lines": 7000,
                    "truncated": True,
                },
            },
        ],
        "project_plan": {
            "revision": 2,
            "goal": _hypothesis(goal_evidence),
            "note": _hypothesis(note_evidence),
            "items": [],
        },
    }


def test_range_provenance_does_not_accept_stale_goal_citation_over_current_note():
    task = _ranged_task(
        goal_evidence=f"{TARGET}:1538-1557 shows the relevant control flow",
        note_evidence=TARGET,
    )

    refined = range_provenance.refine_state(provenance, forced, task, _range_state())

    assert refined["action_kind"] == "evidence"
    assert refined["hypothesis_evidence_range_required"] is True
    assert refined["hypothesis_causal_evidence_linked"] is False


def test_range_provenance_accepts_current_note_despite_stale_goal_text():
    task = _ranged_task(
        goal_evidence=TARGET,
        note_evidence=f"{TARGET}:1538-1557 shows the relevant control flow",
    )

    refined = range_provenance.refine_state(provenance, forced, task, _range_state())

    assert refined["action_kind"] == "edit"
    assert refined["hypothesis_causal_evidence_linked"] is True
    assert refined["hypothesis_causal_evidence_ranges"] == [
        {"path": TARGET, "start_line": 1538, "end_line": 1557}
    ]


def _modified_workspace(name: str, args: dict, result: dict) -> bool:
    if not bool(result.get("ok")):
        return False
    if name == "coding_replace_text":
        return int(result.get("replacements") or 0) > 0
    if name == "coding_apply_patch":
        return not bool(args.get("check_only") or result.get("check_only"))
    return False


def test_failed_check_only_single_file_patch_enters_targeted_recovery():
    call_id = "patch-check-failed"
    task = {
        "agent_events": [
            {
                "type": "tool_finished",
                "name": "coding_update_plan",
                "ts": 10.0,
                "result": {"ok": True, "plan": {"revision": 2}},
            },
            {
                "type": "tool_started",
                "name": "coding_apply_patch",
                "tool_call_id": call_id,
                "cycle": 3,
                "ts": 20.0,
                "args": {
                    "check_only": True,
                    "patch": f"--- a/{TARGET}\n+++ b/{TARGET}\n@@ -1 +1 @@\n-old\n+new\n",
                },
            },
            {
                "type": "tool_finished",
                "name": "coding_apply_patch",
                "tool_call_id": call_id,
                "cycle": 3,
                "ts": 20.0,
                "result": {
                    "ok": False,
                    "check_only": True,
                    "paths": [TARGET],
                    "error": "patch check failed",
                },
            },
        ]
    }
    state = {
        "action_kind": "edit",
        "canonical_action_kind": "edit",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
        "hypothesis_causal_evidence_linked": True,
        "durable_hypothesis_note_updated_at": 10.0,
    }
    agent = SimpleNamespace(
        _tool_result_modified_workspace=_modified_workspace,
        forced_action=None,
    )

    refined = recovery.refine_state(agent, task, state)

    assert refined["action_kind"] == "evidence"
    assert refined["allowed_tools"] == ["coding_finish", "coding_read_file_lines"]
    assert refined["failed_edit_refresh_required"] is True
    assert refined["failed_edit_refresh_target"] == TARGET
    assert refined["failed_edit_refresh_tool"] == "coding_apply_patch"
