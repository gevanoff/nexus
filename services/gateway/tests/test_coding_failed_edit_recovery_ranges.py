from __future__ import annotations

from types import SimpleNamespace

from app import coding_failed_edit_recovery as recovery


TARGET = "services/gateway/app/ui_routes.py"


def _modified_workspace(name: str, args: dict, result: dict) -> bool:
    return bool(
        name == "coding_replace_text"
        and result.get("ok") is True
        and int(result.get("replacements") or 0) > 0
    )


def _failed_replace() -> list[dict]:
    return [
        {
            "type": "tool_started",
            "name": "coding_replace_text",
            "tool_call_id": "replace-1",
            "cycle": 3,
            "ts": 20.0,
            "args": {"path": TARGET, "old_text": "old", "new_text": "new"},
        },
        {
            "type": "tool_finished",
            "name": "coding_replace_text",
            "tool_call_id": "replace-1",
            "cycle": 3,
            "ts": 20.0,
            "result": {
                "ok": False,
                "path": TARGET,
                "replacements": 0,
                "error": "old_text was not found",
            },
        },
    ]


def _plan() -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_update_plan",
        "ts": 10.0,
        "result": {"ok": True, "plan": {"revision": 2}},
    }


def _read(start: int, end: int, *, ts: float = 30.0) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_read_file_lines",
        "ts": ts,
        "result": {
            "ok": True,
            "path": TARGET,
            "start_line": start,
            "end_line": end,
            "line_count": end - start + 1,
            "content": "current source",
        },
    }


def _state() -> dict:
    return {
        "action_kind": "edit",
        "canonical_action_kind": "edit",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
        "durable_hypothesis_note_updated_at": 10.0,
        "causal_evidence_ranges": [
            {"path": TARGET, "start_line": 1600, "end_line": 1650}
        ],
        "hypothesis_causal_evidence_ranges": [
            {"path": TARGET, "start_line": 1610, "end_line": 1640}
        ],
    }


def test_range_qualified_failure_requests_same_hypothesis_span():
    task = {"agent_events": [_plan(), *_failed_replace()]}
    agent = SimpleNamespace(_tool_result_modified_workspace=_modified_workspace)

    state = recovery.refine_state(agent, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["failed_edit_refresh_target"] == TARGET
    # Hypothesis-linked span is more specific and wins over the broader causal span.
    assert state["failed_edit_refresh_start_line"] == 1610
    assert state["failed_edit_refresh_end_line"] == 1640
    assert state["failed_edit_refresh_line_count"] == 31
    assert "start_line=1610" in state["required_action"]
    assert "line_count=31" in state["required_action"]


def test_same_path_read_outside_required_span_does_not_end_recovery():
    task = {"agent_events": [_plan(), *_failed_replace(), _read(1, 100)]}
    agent = SimpleNamespace(_tool_result_modified_workspace=_modified_workspace)

    state = recovery.refine_state(agent, task, _state())

    assert state["action_kind"] == "evidence"
    assert state["failed_edit_refresh_required"] is True
    assert state["failed_edit_refresh_start_line"] == 1610


def test_read_covering_required_span_ends_failed_edit_recovery():
    task = {"agent_events": [_plan(), *_failed_replace(), _read(1600, 1650)]}
    agent = SimpleNamespace(_tool_result_modified_workspace=_modified_workspace)

    state = recovery.refine_state(agent, task, _state())

    assert state["action_kind"] == "edit"
    assert "failed_edit_refresh_required" not in state


def test_runtime_lock_rejects_wrong_span_and_accepts_exact_span():
    class Policy:
        apply_provenance_gate = staticmethod(lambda forced_action, task, state: dict(state))
        _provenance_prompt_context = staticmethod(lambda base, state: "original")

    class Forced:
        def __init__(self) -> None:
            self.base_state = _state()

        def active_state(self, task):
            return Policy.apply_provenance_gate(self, task, dict(self.base_state))

        def evaluate_tool_call(self, task, *, name, args, is_validation_command):
            state = self.active_state(task)
            return name in set(state.get("allowed_tools") or []), {}

    forced = Forced()
    agent = SimpleNamespace(
        _tool_result_modified_workspace=_modified_workspace,
        forced_action=forced,
    )
    recovery.install(agent, Policy)
    task = {"agent_events": [_plan(), *_failed_replace()]}

    allowed, detail = forced.evaluate_tool_call(
        task,
        name="coding_read_file_lines",
        args={"path": TARGET, "start_line": 1, "line_count": 100},
        is_validation_command=lambda argv: False,
    )
    assert allowed is False
    assert detail["error"] == "failed_edit_refresh_range_mismatch"
    assert detail["failed_edit_refresh_start_line"] == 1610
    assert detail["failed_edit_refresh_line_count"] == 31

    allowed, detail = forced.evaluate_tool_call(
        task,
        name="coding_read_file_lines",
        args={"path": TARGET, "start_line": 1610, "line_count": 31},
        is_validation_command=lambda argv: False,
    )
    assert allowed is True
    assert detail == {}
