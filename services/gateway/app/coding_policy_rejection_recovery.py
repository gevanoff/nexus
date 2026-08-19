from __future__ import annotations

import contextvars
import secrets
from types import MethodType
from typing import Any, Mapping


_TRUSTED_DIAGNOSTICS_KEY = "_nexus_coding_policy_rejection_diagnostics"
_CAPTURED_DIAGNOSTICS: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar(
    "nexus_coding_policy_rejection_diagnostics",
    default=(),
)


class _SyntheticPolicyArgs(dict[str, Any]):
    """In-process capability proving that Nexus, not the backend, made this call."""


class _SyntheticPolicyRejectionResult(dict[str, Any]):
    """In-process result type used only for a recovered policy rejection."""


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


def _safe_diagnostics(items: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(items, list):
        return ()
    out: list[dict[str, Any]] = []
    for item in items[:5]:
        if not isinstance(item, Mapping):
            continue
        allowed_raw = item.get("allowed_tool_names")
        allowed = (
            [str(value).strip()[:128] for value in allowed_raw if str(value).strip()]
            if isinstance(allowed_raw, list)
            else []
        )
        out.append(
            {
                "reason": str(item.get("reason") or "").strip()[:120],
                "name": str(item.get("name") or "").strip()[:160],
                "allowed_tool_names": allowed[:20],
            }
        )
    return tuple(out)


def _diagnostics(response: Any) -> list[Mapping[str, Any]]:
    if not isinstance(response, Mapping):
        return []
    raw = response.get(_TRUSTED_DIAGNOSTICS_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _recoverable_policy_diagnostics(
    response: Any,
    *,
    known_tools: set[str],
) -> list[Mapping[str, Any]]:
    recovered: list[Mapping[str, Any]] = []
    for item in _diagnostics(response):
        reason = str(item.get("reason") or "").strip()
        name = str(item.get("name") or "").strip()
        allowed_raw = item.get("allowed_tool_names")
        allowed = (
            {
                str(value).strip()
                for value in allowed_raw
                if str(value).strip()
            }
            if isinstance(allowed_raw, list)
            else set()
        )
        # Only recover a real Nexus Coding tool that was omitted by the current
        # policy-specific schema. Hallucinated/malformed names remain ordinary
        # transport diagnostics and retain the generic no-tool handling path.
        if (
            reason == "unknown tool name"
            and name in known_tools
            and allowed
            and name not in allowed
        ):
            recovered.append(item)
    return recovered


def _synthetic_rejection_call(name: str) -> dict[str, Any]:
    # The call id is only for event correlation. Synthetic provenance is carried
    # by the Python-only `_SyntheticPolicyArgs` type, never by backend-controlled
    # strings, ids, argument keys, or values.
    return {
        "id": f"nexus-policy-rejection-{secrets.token_hex(8)}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": _SyntheticPolicyArgs(),
        },
    }


def _rejection_result(
    forced_action: Any,
    task: Mapping[str, Any],
    *,
    attempted_tool: str,
) -> _SyntheticPolicyRejectionResult:
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
    policy_text = (
        ", ".join(allowed)
        if allowed
        else "no workspace tools from the stale request"
    )
    suffix = (
        f"Required action: {required_action}"
        if required_action
        else "Do not retry the suppressed tool call."
    )
    return _SyntheticPolicyRejectionResult(
        {
            "ok": False,
            "error": "forced_action_tool_rejected",
            "message": (
                f"The backend attempted {attempted_tool or '(missing tool name)'}, but that call was "
                "suppressed by the request's Coding Workspace execution policy before execution. "
                f"Current authorized tools: {policy_text}. {suffix}"
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
    )


def _feedback_message(
    agent: Any,
    *,
    attempted_tool: str,
    result: Mapping[str, Any],
) -> Any:
    allowed = ", ".join(result.get("allowed_tools") or []) or "coding_finish"
    required = str(result.get("required_action") or "").strip()
    suffix = (
        f"Required action: {required}"
        if required
        else "Follow the current controller action now."
    )
    return agent.ChatMessage(
        role="user",
        content=(
            f"Controller policy rejection: {attempted_tool} was NOT executed. Do not retry it "
            "unless it is explicitly advertised in a later execution policy. "
            f"Use exactly one currently authorized tool: {allowed}. {suffix}"
        ),
    )


def _install_trusted_transport_capture(agent: Any) -> None:
    """Capture diagnostics from Nexus' real sanitizer callback for this request.

    The backend response body is untrusted, so recovery must not trust a
    response-provided `_gateway` field claiming that sanitization occurred.
    The callback below observes the diagnostics at the point where Gateway's
    OpenAI sanitizer actually reports them. A ContextVar keeps concurrent
    Coding Workspace requests isolated.
    """
    from app import upstreams

    if not bool(
        getattr(upstreams, "_coding_policy_rejection_capture_installed", False)
    ):
        original_log = upstreams._log_invalid_response_tool_calls

        def log_with_policy_capture(
            diagnostics: list[dict[str, Any]],
            **kwargs: Any,
        ) -> None:
            _CAPTURED_DIAGNOSTICS.set(_safe_diagnostics(diagnostics))
            original_log(diagnostics, **kwargs)

        upstreams._log_invalid_response_tool_calls = log_with_policy_capture
        upstreams._coding_policy_rejection_capture_installed = True

    original_call = getattr(agent, "call_backend_chat", None)
    if not callable(original_call) or bool(
        getattr(agent, "_coding_policy_rejection_call_capture_installed", False)
    ):
        return

    async def call_with_policy_capture(*args: Any, **kwargs: Any) -> Any:
        token = _CAPTURED_DIAGNOSTICS.set(())
        try:
            response = await original_call(*args, **kwargs)
            diagnostics = _CAPTURED_DIAGNOSTICS.get()
        finally:
            _CAPTURED_DIAGNOSTICS.reset(token)
        if not isinstance(response, dict):
            return response
        output = dict(response)
        # Never trust a backend-supplied copy of the private transport field.
        output.pop(_TRUSTED_DIAGNOSTICS_KEY, None)
        if diagnostics:
            output[_TRUSTED_DIAGNOSTICS_KEY] = [dict(item) for item in diagnostics]
        return output

    agent.call_backend_chat = call_with_policy_capture
    agent._coding_policy_rejection_call_capture_installed = True


def install(agent: Any) -> None:
    """Preserve policy-invalid native tool intent across the transport boundary.

    OpenAI response sanitization correctly removes backend tool calls that are
    not present in the request's current tool schema. In a Coding Workspace,
    however, a known tool can be absent *because the controller intentionally
    disabled it*. Converting that attempt into prose loses the policy-rejection
    semantic and trips the generic prose-only failure loop.

    Recovery uses two separate trust boundaries: sanitizer diagnostics must be
    observed in-process, and reconstructed calls carry Python-only argument and
    result types. A backend can imitate their serialized shape, argument keys,
    or call-id text, but cannot manufacture those local object types.
    """
    if bool(getattr(agent, "_coding_policy_rejection_recovery_installed", False)):
        return

    _install_trusted_transport_capture(agent)
    known_tools = _known_coding_tools(agent)
    original_extract = agent._extract_tool_calls
    original_parse = agent._parse_tool_arguments

    def extract_with_policy_recovery(response: Any) -> list[dict[str, Any]]:
        calls = list(original_extract(response))
        diagnostics = _recoverable_policy_diagnostics(
            response,
            known_tools=known_tools,
        )
        if not diagnostics:
            return calls
        # Preserve any genuine advertised calls that survived sanitization while
        # also surfacing the backend's policy-disabled companions to the normal
        # forced-action rejection/noncompliance path. Synthetic provenance stays
        # local even in mixed batches; the real calls remain byte-for-byte intact.
        calls.extend(
            _synthetic_rejection_call(str(item.get("name") or "").strip())
            for item in diagnostics
        )
        return calls

    def parse_with_policy_recovery(raw: Any) -> dict[str, Any]:
        if isinstance(raw, _SyntheticPolicyArgs):
            return raw
        return original_parse(raw)

    agent._extract_tool_calls = extract_with_policy_recovery
    agent._parse_tool_arguments = parse_with_policy_recovery

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
        if isinstance(args, _SyntheticPolicyArgs):
            # Fail closed. Only Nexus can create this in-process argument type;
            # JSON/tool arguments from a backend always parse to an ordinary dict.
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

    def tool_message_with_policy_feedback(
        *,
        tool_call_id: str,
        result: dict[str, Any],
    ) -> Any:
        if isinstance(result, _SyntheticPolicyRejectionResult):
            attempted = str(result.get("attempted_tool") or "").strip()
            return _feedback_message(
                agent,
                attempted_tool=attempted,
                result=result,
            )
        return original_tool_message(tool_call_id=tool_call_id, result=result)

    agent._tool_message_for_result = tool_message_with_policy_feedback
    agent._coding_policy_rejection_recovery_installed = True
