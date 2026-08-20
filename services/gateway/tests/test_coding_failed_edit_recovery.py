from __future__ import annotations

from types import SimpleNamespace

from app import coding_evidence_freshness as freshness
from app import coding_failed_edit_recovery as recovery


TARGET = "services/gateway/app/ui_routes.py"
OTHER = "services/gateway/app/static/image.js"


def _modified_workspace(name: str, args: dict, result: dict) -> bool:
    if not bool(result.get("ok")):
        return False
    if name == "coding_replace_text":
        return int(result.get("replacements") or 0) > 0
    if name == "coding_apply_patch":
        return not bool(args.get("check_only") or result.get("check_only"))
    return False


def _agent(forced_action=None):
    return SimpleNamespace(
        _tool_result_modified_workspace=_modified_workspace,
        forced_action=forced_action,
    )


def _plan(*, ts: float = 10.0, revision: int = 2) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_update_plan",
        "ts": ts,
        "result": {"ok": True, "plan": {"revision": revision}},
    }


def _failed_replace(*, path: str = TARGET, ts: float = 20.0, cycle: int = 3) -> list[dict]:
    call_id = f"replace-{cycle}"
    return [
        {
            "type": "tool_started",
            "name": "coding_replace_text",
            "tool_call_id": call_id,
            "cycle": cycle,
            "ts": ts,
            "args": {
                "path": path,
                "old_text": "stale source",
                "new_text": "replacement",
            },
        },
        {
            "type": "tool_finished",
            "name": "coding_replace_text",
            "tool_call_id": call_id,
            "cycle": cycle,
            "ts": ts,
            "result": {
                "ok": False,
                "path": path,
                "replacements": 0,
                "error": "old_text was not found",
            },
        },
    ]


def _read(path: str = TARGET, *, ts: float = 30.0) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_read_file_lines",
        "ts": ts,
        "result": {"ok": True, "path": path, "content": "current implementation"},
    }


def _edit_state() -> dict:
    return {
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


def test_failed_replace_forces_one_targeted_source_refresh():
    task = {"agent_events": [_plan(), *_failed_replace()]}

    state = recovery.refine_state(_agent(), task, _edit_state())

    assert state["action_kind"] == "evidence"
    assert state["allowed_tools"] == ["coding_finish", "coding_read_file_lines"]
    assert state["failed_edit_refresh_required"] is True
    assert state["failed_edit_refresh_target"] == TARGET
    assert state["failed_edit_refresh_tool"] == "coding_replace_text"
    assert state["failed_edit_refresh_error"] == "old_text was not found"
    assert "Do not retry" in state["required_action"]
    assert TARGET in state["required_action"]


def test_successful_refresh_read_stops_failed_edit_recovery():
    task = {"agent_events": [_plan(), *_failed_replace(), _read()]}

    state = recovery.refine_state(_agent(), task, _edit_state())

    assert state["action_kind"] == "edit"
    assert "failed_edit_refresh_required" not in state


def test_new_plan_revision_after_failure_stops_recovering_old_edit():
    task = {
        "agent_events": [
            _plan(ts=10.0, revision=2),
            *_failed_replace(ts=20.0),
            _read(ts=30.0),
            _plan(ts=40.0, revision=3),
        ]
    }

    state = recovery.refine_state(_agent(), task, _edit_state())

    assert state["action_kind"] == "edit"
    assert "failed_edit_refresh_required" not in state


def test_successful_check_only_patch_is_not_a_failed_edit():
    call_id = "patch-check"
    task = {
        "agent_events": [
            _plan(),
            {
                "type": "tool_started",
                "name": "coding_apply_patch",
                "tool_call_id": call_id,
                "cycle": 3,
                "ts": 20.0,
                "args": {
                    "check_only": True,
                    "patch": f"--- a/{TARGET}\n+++ b/{TARGET}\n",
                },
            },
            {
                "type": "tool_finished",
                "name": "coding_apply_patch",
                "tool_call_id": call_id,
                "cycle": 3,
                "ts": 20.0,
                "result": {"ok": True, "check_only": True, "paths": [TARGET]},
            },
        ]
    }

    state = recovery.refine_state(_agent(), task, _edit_state())

    assert state["action_kind"] == "edit"
    assert "failed_edit_refresh_required" not in state


def test_failed_single_file_patch_requests_refresh_of_that_path():
    call_id = "patch-failed"
    task = {
        "agent_events": [
            _plan(),
            {
                "type": "tool_started",
                "name": "coding_apply_patch",
                "tool_call_id": call_id,
                "cycle": 3,
                "ts": 20.0,
                "args": {
                    "check_only": False,
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
                    "check_only": False,
                    "paths": [TARGET],
                    "error": "patch check failed",
                },
            },
        ]
    }

    state = recovery.refine_state(_agent(), task, _edit_state())

    assert state["action_kind"] == "evidence"
    assert state["failed_edit_refresh_target"] == TARGET
    assert state["failed_edit_refresh_tool"] == "coding_apply_patch"


def test_install_composes_failed_edit_refresh_with_evidence_freshness_and_path_lock():
    class Policy:
        apply_provenance_gate = staticmethod(
            lambda forced_action, task, state: dict(state)
        )
        _provenance_prompt_context = staticmethod(lambda base, state: "original")

    class Forced:
        def __init__(self) -> None:
            self.state = _edit_state()

        def active_state(self, task):
            return Policy.apply_provenance_gate(self, task, dict(self.state))

        def evaluate_tool_call(self, task, *, name, args, is_validation_command):
            state = self.active_state(task)
            return (name in set(state.get("allowed_tools") or []), {})

    forced = Forced()
    agent = _agent(forced)
    freshness.install(Policy)
    recovery.install(agent, Policy)

    failed_task = {
        "project_plan": {"revision": 2, "updated_at": 10.0},
        "agent_events": [_plan(), *_failed_replace()],
    }
    state = forced.active_state(failed_task)
    assert state["action_kind"] == "evidence"
    assert state["failed_edit_refresh_target"] == TARGET

    allowed, detail = forced.evaluate_tool_call(
        failed_task,
        name="coding_read_file_lines",
        args={"path": OTHER, "start_line": 1, "line_count": 100},
        is_validation_command=lambda argv: False,
    )
    assert allowed is False
    assert detail["error"] == "failed_edit_refresh_target_mismatch"
    assert detail["failed_edit_refresh_target"] == TARGET

    allowed, _ = forced.evaluate_tool_call(
        failed_task,
        name="coding_read_file_lines",
        args={"path": TARGET, "start_line": 1, "line_count": 100},
        is_validation_command=lambda argv: False,
    )
    assert allowed is True

    refreshed_task = {
        "project_plan": {"revision": 2, "updated_at": 10.0},
        "agent_events": [_plan(), *_failed_replace(), _read(ts=30.0)],
    }
    refreshed = forced.active_state(refreshed_task)
    assert refreshed["action_kind"] == "evidence"
    assert refreshed["allowed_tools"] == ["coding_finish", "coding_update_plan"]
    assert refreshed["hypothesis_evidence_postdates_plan"] is True
    assert "failed_edit_refresh_required" not in refreshed

    revised_task = {
        "project_plan": {"revision": 3, "updated_at": 40.0},
        "agent_events": [
            _plan(ts=10.0, revision=2),
            *_failed_replace(ts=20.0),
            _read(ts=30.0),
            _plan(ts=40.0, revision=3),
        ],
    }
    revised = forced.active_state(revised_task)
    assert revised["action_kind"] == "edit"
    assert "coding_replace_text" in revised["allowed_tools"]
    assert "failed_edit_refresh_required" not in revised


def test_failed_edit_recovery_prompt_names_exact_refresh_target():
    class Policy:
        apply_provenance_gate = staticmethod(
            lambda forced_action, task, state: dict(state)
        )
        _provenance_prompt_context = staticmethod(lambda base, state: "original")

    class Forced:
        def active_state(self, task):
            return _edit_state()

        def evaluate_tool_call(self, task, *, name, args, is_validation_command):
            return True, {}

    agent = _agent(Forced())
    recovery.install(agent, Policy)
    prompt = Policy._provenance_prompt_context(
        SimpleNamespace(),
        {
            "failed_edit_refresh_required": True,
            "failed_edit_refresh_target": TARGET,
            "failed_edit_refresh_tool": "coding_replace_text",
            "failed_edit_refresh_error": "old_text was not found",
        },
    )

    assert "previous exact edit did not mutate" in prompt
    assert "Do not retry the stale edit" in prompt
    assert TARGET in prompt
    assert "coding_read_file_lines exactly once" in prompt
