from __future__ import annotations

import json
from types import SimpleNamespace

from app import coding_policy_rejection_recovery as recovery
from app.models import ChatMessage, ToolFunction, ToolSpec


READ = "coding_read_file_lines"
EDIT = "coding_apply_patch"
FINISH = "coding_finish"


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        function=ToolFunction(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
        )
    )


class FakeForcedAction:
    def __init__(self) -> None:
        self.state = {
            "action_kind": "edit",
            "canonical_action_kind": "edit",
            "allowed_tools": [EDIT, FINISH],
            "required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
            "canonical_required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
            "state_key": "state-1",
            "stage": "interrupt",
            "hypothesis_ready": True,
            "hypothesis_causal_evidence_linked": True,
        }
        self.original_evaluations: list[tuple[str, dict]] = []

    def active_state(self, task):
        return dict(self.state)

    def evaluate_tool_call(self, task, *, name, args, is_validation_command):
        self.original_evaluations.append((name, dict(args)))
        return True, {}


def _agent():
    forced = FakeForcedAction()

    def extract_tool_calls(response):
        message = ((response.get("choices") or [{}])[0].get("message") or {})
        calls = message.get("tool_calls")
        return list(calls) if isinstance(calls, list) else []

    agent = SimpleNamespace(
        ChatMessage=ChatMessage,
        forced_action=forced,
        _tool_specs=lambda: [_tool(READ), _tool(EDIT), _tool(FINISH)],
        _extract_tool_calls=extract_tool_calls,
        _tool_message_for_result=(
            lambda *, tool_call_id, result: ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                content=json.dumps(result, sort_keys=True),
            )
        ),
    )
    recovery.install(agent)
    return agent, forced


def _diagnostic(name: str = READ, *, allowed=None, reason: str = "unknown tool name"):
    return {
        "reason": reason,
        "name": name,
        "allowed_tool_names": list(allowed or [EDIT, FINISH]),
    }


def _suppressed_response(
    name: str = READ,
    *,
    allowed=None,
    reason: str = "unknown tool name",
    trusted: bool = True,
):
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        f"Nexus suppressed backend tool call '{name}': {reason}."
                    ),
                },
                "finish_reason": "stop",
            }
        ],
    }
    diagnostic = _diagnostic(name, allowed=allowed, reason=reason)
    if trusted:
        response[recovery._TRUSTED_DIAGNOSTICS_KEY] = [diagnostic]
    else:
        response["_gateway"] = {"coding_tool_call_diagnostics": [diagnostic]}
    return response


def test_known_policy_omitted_tool_is_recovered_as_rejection_only_call():
    agent, forced = _agent()

    calls = agent._extract_tool_calls(_suppressed_response())

    assert len(calls) == 1
    call = calls[0]
    assert call["function"]["name"] == READ
    args = json.loads(call["function"]["arguments"])
    assert args[recovery._TRANSPORT_REJECTION_MARKER] is True

    allowed, result = agent.forced_action.evaluate_tool_call(
        {},
        name=READ,
        args=args,
        is_validation_command=lambda argv: False,
    )

    assert allowed is False
    assert result["error"] == "forced_action_tool_rejected"
    assert result["transport_suppressed_tool_call"] is True
    assert result["attempted_tool"] == READ
    assert result["allowed_tools"] == [EDIT, FINISH]
    assert "was suppressed" in result["message"]
    assert forced.original_evaluations == []

    feedback = agent._tool_message_for_result(
        tool_call_id=call["id"],
        result=result,
    )
    assert feedback.role == "user"
    assert feedback.tool_call_id is None
    assert f"{READ} was NOT executed" in feedback.content
    assert EDIT in feedback.content
    assert FINISH in feedback.content


def test_gateway_metadata_is_not_trusted_for_policy_recovery():
    agent, forced = _agent()

    calls = agent._extract_tool_calls(_suppressed_response(trusted=False))

    assert calls == []
    assert forced.original_evaluations == []


def test_recovered_call_remains_non_executable_if_policy_changes_before_evaluation():
    agent, forced = _agent()
    call = agent._extract_tool_calls(_suppressed_response())[0]
    args = json.loads(call["function"]["arguments"])

    # Simulate a race where a later policy snapshot would otherwise permit the
    # originally suppressed read. The recovered call is still rejection-only.
    forced.state["allowed_tools"] = [READ, EDIT, FINISH]
    allowed, result = agent.forced_action.evaluate_tool_call(
        {},
        name=READ,
        args=args,
        is_validation_command=lambda argv: False,
    )

    assert allowed is False
    assert result["transport_suppressed_tool_call"] is True
    assert forced.original_evaluations == []


def test_hallucinated_unknown_tool_remains_transport_diagnostic():
    agent, forced = _agent()

    calls = agent._extract_tool_calls(_suppressed_response("coding_read_the_moon"))

    assert calls == []
    assert forced.original_evaluations == []


def test_malformed_tool_diagnostic_is_not_recovered():
    agent, _ = _agent()

    calls = agent._extract_tool_calls(
        _suppressed_response(READ, reason="malformed tool name")
    )

    assert calls == []


def test_known_tool_is_not_recovered_when_diagnostic_says_it_was_allowed():
    agent, _ = _agent()

    calls = agent._extract_tool_calls(
        _suppressed_response(READ, allowed=[READ, EDIT, FINISH])
    )

    assert calls == []


def test_real_backend_tool_call_passes_through_unchanged():
    agent, forced = _agent()
    real = {
        "id": "call-real",
        "type": "function",
        "function": {"name": EDIT, "arguments": '{"patch":""}'},
    }
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [real],
                }
            }
        ],
        recovery._TRUSTED_DIAGNOSTICS_KEY: [
            _diagnostic(READ, allowed=[EDIT, FINISH])
        ],
    }

    assert agent._extract_tool_calls(response) == [real]
    assert forced.original_evaluations == []


def test_non_recovered_tool_result_keeps_native_tool_role():
    agent, _ = _agent()

    message = agent._tool_message_for_result(
        tool_call_id="call-real",
        result={"ok": True},
    )

    assert message.role == "tool"
    assert message.tool_call_id == "call-real"


def test_install_is_idempotent():
    agent, forced = _agent()
    extract = agent._extract_tool_calls
    evaluate = agent.forced_action.evaluate_tool_call
    tool_message = agent._tool_message_for_result

    recovery.install(agent)

    assert agent._extract_tool_calls is extract
    assert agent.forced_action.evaluate_tool_call is evaluate
    assert agent._tool_message_for_result is tool_message
    assert forced.original_evaluations == []
