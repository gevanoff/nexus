from __future__ import annotations

import pytest

from app import coding_evidence_policy as provenance
from app import coding_execution_dispatch as dispatch
from app import coding_forced_action as forced


def _structured_plan(repository_evidence: str) -> dict:
    return {
        "revision": 1,
        "goal": "Repair the regression",
        "items": [],
        "note": (
            "Root cause: the implementation path is not producing the expected behavior.\n"
            f"Repository evidence: {repository_evidence}\n"
            "Competing explanation checked: configuration-only failure was checked.\n"
            "Expected result: the configured behavior is restored."
        ),
    }


def _activate_acceptance_only_task(path: str) -> dict:
    task = {
        "agent_run_id": "run-review",
        "agent_cycle": 6,
        "project_plan": {"revision": 0, "goal": "repair", "items": [], "note": ""},
        "agent_events": [],
    }
    task["agent_forced_action"] = forced.activate(
        task,
        state_key="unchanged-state",
        run_id="run-review",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )
    activated = float(task["agent_forced_action"].get("activated_at") or 0)
    task["agent_events"] = [
        {
            "ts": activated + 1,
            "type": "tool_started",
            "tool_call_id": "read-1",
            "name": "coding_read_file_lines",
            "args": {"path": path, "start_line": 1, "line_count": 40},
        },
        {
            "ts": activated + 2,
            "type": "tool_finished",
            "tool_call_id": "read-1",
            "name": "coding_read_file_lines",
            "result": {"path": path, "content": "acceptance fixture"},
        },
    ]
    task["project_plan"] = _structured_plan(path)
    return task


def test_mapping_copy_helpers_do_not_call_dict_copy_with_update_keyword():
    message = {"role": "system", "content": "old"}
    copied_message = dispatch._copy_message(message, content="new")

    assert copied_message == {"role": "system", "content": "new"}
    assert message == {"role": "system", "content": "old"}

    request = {"model": "coder", "max_tokens": 128}
    copied_request = dispatch._copy_request(request, max_tokens=64)

    assert copied_request == {"model": "coder", "max_tokens": 64}
    assert request == {"model": "coder", "max_tokens": 128}


@pytest.mark.parametrize(
    "path",
    [
        "services/telegram-bot/healthcheck.test.js",
        "web/foo.spec.ts",
        "internal/foo_test.go",
        "ui/component.spec.tsx",
        "pkg/worker_test.rs",
    ],
)
def test_conventional_colocated_test_names_are_acceptance_evidence(path: str):
    assert provenance._path_class(path) == "acceptance"


def test_execution_authorization_enforces_provenance_without_mutating_base_controller():
    path = "services/telegram-bot/healthcheck.test.js"
    task = _activate_acceptance_only_task(path)

    # The legacy/base controller sees one successful targeted read plus a
    # structured hypothesis and therefore reaches edit scope.
    base_state = forced.active_state(task)
    assert base_state["action_kind"] == "edit"
    base_allowed, _ = forced.evaluate_tool_call(
        task,
        name="coding_apply_patch",
        args={"patch": ""},
        is_validation_command=lambda _argv: False,
    )
    assert base_allowed is True

    # The Coding Agent facade refines that same durable state with provenance
    # and must enforce it again when the model actually attempts a tool call.
    class Agent:
        pass

    agent = Agent()
    agent.forced_action = forced
    provenance.install_execution_override_seam(agent)

    effective = agent.forced_action.active_state(task)
    assert effective["action_kind"] == "evidence"
    assert effective["acceptance_evidence_targets"] == [path]
    assert effective["causal_evidence_targets"] == []

    allowed, rejection = agent.forced_action.evaluate_tool_call(
        task,
        name="coding_apply_patch",
        args={"patch": ""},
        is_validation_command=lambda _argv: False,
    )

    assert allowed is False
    assert rejection["error"] == "forced_action_tool_rejected"
    assert "coding_apply_patch" not in rejection["allowed_tools"]
    assert rejection["hypothesis_causal_evidence_linked"] is False

    # Installing the Coding Agent facade does not globally alter the reusable
    # controller module's legacy contract for ordinary callers/tests.
    base_allowed_after, _ = forced.evaluate_tool_call(
        task,
        name="coding_apply_patch",
        args={"patch": ""},
        is_validation_command=lambda _argv: False,
    )
    assert base_allowed_after is True
