from __future__ import annotations

import json
import secrets
from types import MethodType
from typing import Any, Mapping


_TRANSPORT_REJECTION_MARKER = "_nexus_transport_policy_rejection"
_CALL_ID_PREFIX = "nexus-policy-rejection-"


def _tool_name(spec: Any) -> str:
    function = getattr(spec, "function", None)
    if function is None and isinstance(spec, Mapping):
        function = spec.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(getattr(function, "name", "") or "").strip()


def _known_coding_tools(agent: Any) -> set[str]:
    specs = getattr(agent, "_tool_specs", None)
    if not callable(specs):
        return set()
    try:
        return {
            name
            for name in (_tool_name(spec) for spec in specs())
            if name
        }
    except Exception:
        return set()


def _diagnostics(response: Any) -> list[Mapping[str, Any]]:
    if not isinstance(response, Mapping):
        return []
    gateway = response.get("_gateway")
    if not isinstance(gateway, Mapping):
        return []
    raw = gateway.get("coding_tool_call_diagnostics")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _recoverable_policy_diagnostic(
    response: Any,
    *,
    known_tools: set[str],
) -> Mapping[str, Any] | None:
    for item in _diagnostics(response):
        reason = str(item.get("reason") or "").strip()
        name = str(item.get("name") or "").strip()
        allowed_raw = item.get("allowed_tool_names")
        allowed = {
            str(value).strip()
            for value in allowed_raw
            if str(value).strip()
        } if isinstance(allowed_raw, list) else set()
        # Only recover a real Nexus Coding tool that was omitted by the current
        # policy-specific schema. Hallucinated/malformed names remain ordinary
        # transport diagnostics and retain the generic no-tool handling path.
        if (
            reason == "unknown tool name"
            and name in known_tools
            and allowed
            and name not in allowed
        ):
            return item
    return None


def _synthetic_rejection_call(name: str) -> dict[str, Any]:
    return {
        "id": f"{_CALL_ID_PREFIX}{secrets.token_hex(8)}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                {_TRANSPORT_REJECTION_MARKER: True},
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    }


def _rejection_result(
    forced_action: Any,
    task: Mapping[str, Any],
    *,
    attempted_tool: str,
) -> dict[str, Any]:
    state = forced_action.active_state(task)
    state = state if isinstance(state, Mapping) else {}
    allowed = sorted(
        {
            str(value).strip()
            for value in (state.get("allowed_tools") or [])
            if str(value).strip()
        }
    )
    required_action = str(state.get("required_action") or "").strip()
    policy_text = ", ".join(allowed) if allowed else "no workspace tools from the stale request"
    return {
        "ok": False,
        "error": "forced_action_tool_rejected",
        "message": (
            f"The backend attempted {attempted_tool or '(missing tool name)'}, but that call was "
            "suppressed by the request's Coding Workspace execution policy before execution. "
            f"Current authorized tools: {policy_text}. "
            + (f"Required action: {required_action}" if required_action else "Do not retry the suppressed tool call.")
        ),
        "required_action": required_action,
        "canonical_required_action": state.get("canonical_required_action"),
        "action_kind": state.get("action_kind"),
        "canonical_action_kind": state.get("canonical_action_kind"),
        "allowed_tools": allowed,
        "state_key": state.get("state_key"),
        "stage": state.get("stage"),
        "hypothesis_ready": state.get("hypothesis_ready"),
        "hypothesis_causal_evidence_linked": bool(
            state.get("hypothesis_causal_evidence_linked")
        ),
        "transport_suppressed_tool_call": True,
        "attempted_tool": attempted_tool,
    }


def _feedback_message(agent: Any, *, attempted_tool: str, result: Mapping[str, Any]) -> Any:
    allowed = ", ".join(result.get("allowed_tools") or []) or "coding_finish"
    required = str(result.get("required_action") or "").strip()
    return agent.ChatMessage(
        role="user",
        content=(
            f"Controller policy rejection: {attempted_tool} was NOT executed. Do not retry it "
            "unless it is explicitly advertised in a later execution policy. "
            f"Use exactly one currently authorized tool: {allowed}. "
            + (f"Required action: {required}" if required else "Follow the current controller action now.")
        ),
    )


def install(agent: Any) -> None:
    """Preserve policy-invalid native tool intent across the transport boundary.

    OpenAI response sanitization correctly removes backend tool calls that are
    not present in the request's current tool schema. In a Coding Workspace,
    however, a known tool can be absent *because the controller intentionally
    disabled it*. Converting that attempt into prose loses the policy-rejection
    semantic and trips the generic prose-only failure loop.

    This overlay reconstructs only that narrow case as a rejection-only call.
    A private marker forces rejection before workspace execution even if policy
    changes between response parsing and tool evaluation. The resulting feedback
    is returned as user-role controller data rather than an orphan tool message,
    so strict native-tool backends never receive history for an unadvertised tool.
    """
    if bool(getattr(agent, "_coding_policy_rejection_recovery_installed", False)):
        return

    known_tools = _known_coding_tools(agent)
    original_extract = agent._extract_tool_calls

    def extract_with_policy_recovery(response: Any) -> list[dict[str, Any]]:
        calls = original_extract(response)
        if calls:
            return calls
        diagnostic = _recoverable_policy_diagnostic(
            response,
            known_tools=known_tools,
        )
        if diagnostic is None:
            return []
        name = str(diagnostic.get("name") or "").strip()
        return [_synthetic_rejection_call(name)]

    agent._extract_tool_calls = extract_with_policy_recovery

    forced_action = agent.forced_action
    original_evaluate = forced_action.evaluate_tool_call

    def evaluate_with_transport_rejection(
        self: Any,
        task: Mapping[str, Any],
        *,
        name: str,
        args: Mapping[str, Any],
        is_validation_command: Any,
    ) -> tuple[bool, dict[str, Any]]:
        if bool(args.get(_TRANSPORT_REJECTION_MARKER)):
            # Fail closed. This recovered call exists only to preserve the
            # controller rejection semantic and must never reach _run_tool.
            return False, _rejection_result(
                self,
                task,
                attempted_tool=str(name or "").strip(),
            )
        return original_evaluate(
            task,
            name=name,
            args=args,
            is_validation_command=is_validation_command,
        )

    forced_action.evaluate_tool_call = MethodType(
        evaluate_with_transport_rejection,
        forced_action,
    )

    original_tool_message = agent._tool_message_for_result

    def tool_message_with_policy_feedback(*, tool_call_id: str, result: dict[str, Any]) -> Any:
        if str(tool_call_id or "").startswith(_CALL_ID_PREFIX):
            attempted = str(result.get("attempted_tool") or "").strip()
            return _feedback_message(
                agent,
                attempted_tool=attempted,
                result=result,
            )
        return original_tool_message(tool_call_id=tool_call_id, result=result)

    agent._tool_message_for_result = tool_message_with_policy_feedback
    agent._coding_policy_rejection_recovery_installed = True
