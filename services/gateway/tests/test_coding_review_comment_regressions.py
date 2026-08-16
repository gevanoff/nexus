from __future__ import annotations

import asyncio

import pytest

from app import coding_evidence_policy as provenance
from app import coding_execution_dispatch as dispatch
from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience


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
        state_key=resilience.durable_state_key(task),
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


def test_fully_mapping_shaped_coding_request_is_detected_and_copied():
    request = {
        "model": "coder",
        "messages": [
            {"role": "system", "content": "You are Nexus Coding Agent."},
            {"role": "user", "content": "Continue."},
        ],
        "max_tokens": 128,
    }

    assert dispatch._is_coding_execution_request(request) is True
    assert dispatch._request_value(request, "model") == "coder"
    copied = dispatch._copy_request(request, max_tokens=64)
    assert copied["messages"] == request["messages"]
    assert copied["max_tokens"] == 64


def test_converted_tool_result_keeps_text_tool_continuation_instruction():
    class Message:
        def __init__(
            self,
            *,
            role: str,
            content: str | None = None,
            tool_calls=None,
            tool_call_id: str | None = None,
        ) -> None:
            self.role = role
            self.content = content
            self.tool_calls = tool_calls
            self.tool_call_id = tool_call_id

    class Agent:
        ChatMessage = Message

        @staticmethod
        def _tool_context_char_limit() -> int:
            return 4000

        @staticmethod
        def _clip_text(value: str, limit: int) -> str:
            return value if len(value) <= limit else value[: limit - 1] + "…"

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "coding_read_file_lines",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "read result",
        },
    ]

    normalized, diagnostics = dispatch._normalize_messages(
        Agent(),
        messages,
        text_tool_mode=True,
        fresh_system="system",
    )

    user_messages = [
        item.content
        for item in normalized
        if getattr(item, "role", "") == "user"
    ]
    assert diagnostics["converted_tool_results"] == 1
    assert any(
        "Continue the coding task with exactly one complete <tool_call>{...}</tool_call> block"
        in str(content)
        for content in user_messages
    )


def test_non_coding_dispatch_does_not_record_execution_policy_transition():
    class Agent:
        @staticmethod
        def _mutate_task(*_args, **_kwargs):
            raise AssertionError("non-coding dispatch must not mutate execution policy")

        @staticmethod
        def _append_event(*_args, **_kwargs):
            raise AssertionError("non-coding dispatch must not append policy events")

    asyncio.run(
        dispatch._record_policy_transition(
            Agent(),
            object(),
            "code_review",
            task={},
            snapshot=None,
            diagnostics={"coding_request": False},
            cycle=1,
        )
    )


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


def test_causal_evidence_link_requires_repository_relative_target_not_basename_only():
    target = "services/gateway/app/config.py"

    assert provenance._repository_evidence_links_target(
        "services/gateway/app/config.py contains the failing route configuration",
        target,
    )
    assert not provenance._repository_evidence_links_target(
        "config.py contains the failing route configuration",
        target,
    )
    assert not provenance._repository_evidence_links_target(
        "services/images/app/config.py contains an unrelated configuration",
        target,
    )


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
