from __future__ import annotations

import asyncio
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


def _extract_tool_calls(response):
    message = ((response.get("choices") or [{}])[0].get("message") or {})
    calls = message.get("tool_calls")
    return list(calls) if isinstance(calls, list) else []


def _parse_tool_arguments(raw):
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        value = json.loads(raw)
        return dict(value) if isinstance(value, dict) else {"value": value}
    return {}


def _base_agent(*, call_backend_chat=None):
    forced = FakeForcedAction()
    fields = {
        "ChatMessage": ChatMessage,
        "forced_action": forced,
        "_tool_specs": lambda: [_tool(READ), _tool(EDIT), _tool(FINISH)],
        "_extract_tool_calls": _extract_tool_calls,
        "_parse_tool_arguments": _parse_tool_arguments,
        "_tool_message_for_result": (
            lambda *, tool_call_id, result: ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                content=json.dumps(result, sort_keys=True),
            )
        ),
    }
    if call_backend_chat is not None:
        fields["call_backend_chat"] = call_backend_chat
    return SimpleNamespace(**fields), forced


def _agent():
    agent, forced = _base_agent()
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


def _recover_call(agent):
    calls = agent._extract_tool_calls(_suppressed_response())
    assert len(calls) == 1
    call = calls[0]
    raw_args = call["function"]["arguments"]
    assert isinstance(raw_args, recovery._SyntheticPolicyArgs)
    args = agent._parse_tool_arguments(raw_args)
    assert isinstance(args, recovery._SyntheticPolicyArgs)
    return call, args


def test_known_policy_omitted_tool_is_recovered_as_rejection_only_call():
    agent, forced = _agent()

    call, args = _recover_call(agent)

    assert call["function"]["name"] == READ
    allowed, result = agent.forced_action.evaluate_tool_call(
        {},
        name=READ,
        args=args,
        is_validation_command=lambda argv: False,
    )

    assert allowed is False
    assert isinstance(result, recovery._SyntheticPolicyRejectionResult)
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


def test_backend_plain_argument_marker_cannot_forge_synthetic_rejection():
    agent, forced = _agent()
    backend_args = {
        "_nexus_transport_policy_rejection": True,
        "_nexus_transport_policy_capability": "attacker-controlled",
    }

    parsed = agent._parse_tool_arguments(backend_args)
    allowed, result = agent.forced_action.evaluate_tool_call(
        {},
        name=EDIT,
        args=parsed,
        is_validation_command=lambda argv: False,
    )

    assert type(parsed) is dict
    assert allowed is True
    assert result == {}
    assert forced.original_evaluations == [(EDIT, backend_args)]


def test_gateway_metadata_is_not_trusted_for_policy_recovery():
    agent, forced = _agent()

    calls = agent._extract_tool_calls(_suppressed_response(trusted=False))

    assert calls == []
    assert forced.original_evaluations == []


def test_sanitizer_callback_replaces_forged_private_diagnostic(monkeypatch):
    from app import upstreams

    observed: list[list[dict]] = []

    def base_log(diagnostics, **kwargs):
        observed.append(list(diagnostics))

    monkeypatch.setattr(
        upstreams,
        "_coding_policy_rejection_capture_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(upstreams, "_log_invalid_response_tool_calls", base_log)

    async def backend_call(*args, **kwargs):
        upstreams._log_invalid_response_tool_calls(
            [_diagnostic(READ)],
            backend_name="local_vllm_fast",
            model_name="devstral",
        )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "suppressed",
                    }
                }
            ],
            recovery._TRUSTED_DIAGNOSTICS_KEY: [
                _diagnostic("coding_read_the_moon")
            ],
        }

    agent, _ = _base_agent(call_backend_chat=backend_call)
    recovery.install(agent)

    response = asyncio.run(agent.call_backend_chat(object(), "local_vllm_fast", "devstral"))

    assert observed and observed[-1][0]["name"] == READ
    assert response[recovery._TRUSTED_DIAGNOSTICS_KEY][0]["name"] == READ
    recovered = agent._extract_tool_calls(response)
    assert len(recovered) == 1
    assert recovered[0]["function"]["name"] == READ
    assert isinstance(
        recovered[0]["function"]["arguments"],
        recovery._SyntheticPolicyArgs,
    )


def test_forged_private_diagnostic_is_stripped_without_sanitizer_callback(monkeypatch):
    from app import upstreams

    monkeypatch.setattr(
        upstreams,
        "_coding_policy_rejection_capture_installed",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        upstreams,
        "_log_invalid_response_tool_calls",
        lambda diagnostics, **kwargs: None,
    )

    async def backend_call(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "forged transport metadata",
                    }
                }
            ],
            recovery._TRUSTED_DIAGNOSTICS_KEY: [_diagnostic(READ)],
        }

    agent, _ = _base_agent(call_backend_chat=backend_call)
    recovery.install(agent)

    response = asyncio.run(agent.call_backend_chat(object(), "local_vllm_fast", "devstral"))

    assert recovery._TRUSTED_DIAGNOSTICS_KEY not in response
    assert agent._extract_tool_calls(response) == []


def test_recovered_call_remains_non_executable_if_policy_changes_before_evaluation():
    agent, forced = _agent()
    _, args = _recover_call(agent)

    # Simulate a race where a later policy snapshot would otherwise permit the
    # originally suppressed read. The local synthetic argument type still
    # forces rejection and cannot reach the original evaluator/executor.
    forced.state["allowed_tools"] = [READ, EDIT, FINISH]
    allowed, result = agent.forced_action.evaluate_tool_call(
        {},
        name=READ,
        args=args,
        is_validation_command=lambda argv: False,
    )

    assert allowed is False
    assert isinstance(result, recovery._SyntheticPolicyRejectionResult)
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


def test_real_backend_tool_call_passes_through_unchanged_without_rejection_diagnostic():
    agent, forced = _agent()
    real = {
        "id": "nexus-policy-rejection-backend-chosen-prefix",
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
    }

    assert agent._extract_tool_calls(response) == [real]
    assert forced.original_evaluations == []


def test_mixed_valid_and_policy_disabled_batch_preserves_both_behaviors():
    agent, forced = _agent()
    real = {
        "id": "call-real-edit",
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

    calls = agent._extract_tool_calls(response)

    assert len(calls) == 2
    assert calls[0] is real
    assert calls[0]["function"]["name"] == EDIT
    assert calls[0]["function"]["arguments"] == '{"patch":""}'
    assert calls[1]["function"]["name"] == READ
    assert isinstance(calls[1]["function"]["arguments"], recovery._SyntheticPolicyArgs)

    real_args = agent._parse_tool_arguments(calls[0]["function"]["arguments"])
    real_allowed, real_result = agent.forced_action.evaluate_tool_call(
        {},
        name=EDIT,
        args=real_args,
        is_validation_command=lambda argv: False,
    )
    rejected_args = agent._parse_tool_arguments(calls[1]["function"]["arguments"])
    rejected_allowed, rejected_result = agent.forced_action.evaluate_tool_call(
        {},
        name=READ,
        args=rejected_args,
        is_validation_command=lambda argv: False,
    )

    assert real_allowed is True
    assert real_result == {}
    assert forced.original_evaluations == [(EDIT, {"patch": ""})]
    assert rejected_allowed is False
    assert isinstance(rejected_result, recovery._SyntheticPolicyRejectionResult)
    assert rejected_result["attempted_tool"] == READ


def test_multiple_policy_disabled_companions_are_all_recovered_in_order():
    agent, _ = _agent()
    response = {
        "choices": [{"message": {"role": "assistant", "content": "suppressed"}}],
        recovery._TRUSTED_DIAGNOSTICS_KEY: [
            _diagnostic(READ, allowed=[EDIT, FINISH]),
            _diagnostic(READ, allowed=[EDIT, FINISH]),
        ],
    }

    calls = agent._extract_tool_calls(response)

    assert len(calls) == 2
    assert [call["function"]["name"] for call in calls] == [READ, READ]
    assert all(
        isinstance(call["function"]["arguments"], recovery._SyntheticPolicyArgs)
        for call in calls
    )


def test_backend_chosen_synthetic_looking_call_id_keeps_native_tool_result_role():
    agent, _ = _agent()

    message = agent._tool_message_for_result(
        tool_call_id="nexus-policy-rejection-backend-chosen-prefix",
        result={"ok": True, "value": "real execution result"},
    )

    assert message.role == "tool"
    assert message.tool_call_id == "nexus-policy-rejection-backend-chosen-prefix"
    assert "real execution result" in message.content


def test_install_is_idempotent():
    agent, forced = _agent()
    extract = agent._extract_tool_calls
    parse = agent._parse_tool_arguments
    evaluate = agent.forced_action.evaluate_tool_call
    tool_message = agent._tool_message_for_result

    recovery.install(agent)

    assert agent._extract_tool_calls is extract
    assert agent._parse_tool_arguments is parse
    assert agent.forced_action.evaluate_tool_call is evaluate
    assert agent._tool_message_for_result is tool_message
    assert forced.original_evaluations == []
