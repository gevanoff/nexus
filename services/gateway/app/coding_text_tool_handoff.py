from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


_TOOL_CONTRACT_BUDGET_CHARS = 20_000
_TEXT_TOOL_CODING_CAP = 2_048
_CODING_LOGICAL_MODELS = {"coder", "long"}


def _strip_schema_prose(value: Any) -> Any:
    """Keep executable JSON-schema structure without leaking unavailable tools.

    Tool descriptions can mention follow-on tools (for example coding_finish may
    describe coding_git_diff). A text backend must see the exact argument shape
    for authorized tools, but descriptions of currently disallowed tools weaken
    the controller contract and confuse smaller fallback models.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _strip_schema_prose(child)
            for key, child in value.items()
            if str(key) not in {"description", "title", "examples"}
        }
    if isinstance(value, list):
        return [_strip_schema_prose(item) for item in value]
    return value


def _function_payload(spec: Any) -> dict[str, Any]:
    function = getattr(spec, "function", None)
    if function is None and isinstance(spec, Mapping):
        function = spec.get("function")
    if function is None:
        return {}
    if hasattr(function, "model_dump"):
        raw = function.model_dump(exclude_none=True)
    elif isinstance(function, Mapping):
        raw = dict(function)
    else:
        raw = {
            "name": getattr(function, "name", ""),
            "parameters": getattr(function, "parameters", None),
        }
    if not isinstance(raw, Mapping):
        return {}
    name = str(raw.get("name") or "").strip()
    if not name:
        return {}
    parameters = raw.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {"type": "object", "properties": {}}
    return {
        "name": name,
        "parameters": _strip_schema_prose(dict(parameters)),
    }


def _serialize_contracts(specs: Sequence[Any]) -> str:
    contracts = [_function_payload(spec) for spec in specs]
    contracts = [item for item in contracts if item]
    contracts.sort(key=lambda item: item["name"])
    payload = json.dumps(contracts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(payload) <= _TOOL_CONTRACT_BUDGET_CHARS:
        return payload

    # Forced-action policies normally expose only a handful of tools. If an
    # unrestricted contract catalog is unusually large, preserve every tool
    # name and top-level argument type/required list rather than clipping JSON
    # mid-token into an unusable schema.
    compact = []
    for item in contracts:
        parameters = item.get("parameters") if isinstance(item.get("parameters"), Mapping) else {}
        properties = parameters.get("properties") if isinstance(parameters.get("properties"), Mapping) else {}
        compact.append(
            {
                "name": item["name"],
                "parameters": {
                    "type": parameters.get("type", "object"),
                    "properties": {
                        str(name): {"type": schema.get("type", "string")}
                        if isinstance(schema, Mapping)
                        else {"type": "string"}
                        for name, schema in properties.items()
                    },
                    "required": list(parameters.get("required") or []),
                },
            }
        )
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _text_tool_contract_context(agent: Any, task: Mapping[str, Any]) -> str:
    specs = list(agent._tool_specs_for_task(dict(task)))
    contracts = _serialize_contracts(specs)
    names = [
        str(item.get("name") or "")
        for item in (_function_payload(spec) for spec in specs)
        if item.get("name")
    ]
    return (
        "\n\nText-tool workspace contracts (authoritative for this turn): "
        "This backend is continuing the same Nexus Coding Workspace used by native-tool backends. "
        "The tools below execute against the same repository, durable project plan, validation environment, and controller state. "
        "Only these currently authorized tools may be called. Emit exactly one complete "
        "<tool_call>{\"name\":\"...\",\"arguments\":{...}}</tool_call> block and no prose. "
        "Arguments must satisfy the corresponding JSON schema. Do not invent unavailable tools. "
        f"Authorized tool names: {', '.join(sorted(names)) or 'coding_finish'}. "
        f"Contracts JSON: {contracts}"
    )


def _is_vllm_text_handoff(model: str, backend: str, agent: Any) -> bool:
    return (
        str(model or "").strip().casefold() in _CODING_LOGICAL_MODELS
        and str(backend or "").strip().startswith("local_vllm")
        and not agent._backend_supports_tool_calling(backend)
    )


def _install_transport_cap_override() -> None:
    """Preserve the Coding Workspace handoff cap through generic alias routing.

    The generic OpenAI router intentionally applies the most conservative cap
    shared by aliases for a raw backend/model pair. That is correct for ordinary
    chat but can shrink a rematerialized Coding Workspace turn after dispatch has
    already selected a larger bounded text-tool cap. Restore only the requested
    `coder`/`long` vLLM handoff cap, never more than 2048 tokens.
    """
    from app import upstreams

    if bool(getattr(upstreams, "_coding_text_tool_cap_override_installed", False)):
        return
    original_route = upstreams.route_request_for_backend

    def route_request_for_backend(req: Any, backend_name: str, model_name: str):
        routed = original_route(req, backend_name, model_name)
        logical_model = str(getattr(req, "model", "") or "").strip().casefold()
        if logical_model not in _CODING_LOGICAL_MODELS:
            return routed
        if not str(backend_name or "").strip().startswith("local_vllm"):
            return routed
        try:
            requested = int(getattr(req, "max_tokens", 0) or 0)
        except (TypeError, ValueError):
            return routed
        if requested <= 0:
            return routed
        desired = min(requested, _TEXT_TOOL_CODING_CAP)
        try:
            current = int(getattr(routed, "max_tokens", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        if current >= desired:
            return routed
        return routed.model_copy(update={"max_tokens": desired})

    upstreams._route_request_for_backend_before_coding_text_handoff = original_route
    upstreams.route_request_for_backend = route_request_for_backend
    upstreams._coding_text_tool_cap_override_installed = True


def install(agent: Any) -> None:
    """Make text-tool coding backends capable peers of native-tool routes.

    Coding Workspace keeps execution in the Gateway; vLLM models need only emit
    the same authorized workspace calls. This shim gives them exact current tool
    contracts and enough bounded output budget to express edits/plan updates
    after an MLX failover, without enabling native tool transport on backends
    configured for text-form tool use.
    """
    _install_transport_cap_override()
    if bool(getattr(agent, "_text_tool_handoff_installed", False)):
        return

    original_system_prompt = agent._system_prompt
    original_max_tokens = agent._max_completion_tokens_for_route

    def system_prompt(task: Mapping[str, Any], *, text_tool_mode: bool = False) -> str:
        prompt = original_system_prompt(dict(task), text_tool_mode=text_tool_mode)
        if not text_tool_mode:
            return prompt
        return prompt + _text_tool_contract_context(agent, task)

    def max_completion_tokens_for_route(
        model: str,
        backend: str,
        upstream_model: str = "",
    ) -> int:
        original = int(original_max_tokens(model, backend, upstream_model))
        if not _is_vllm_text_handoff(model, backend, agent):
            return original
        return max(original, _TEXT_TOOL_CODING_CAP)

    agent._original_system_prompt_before_text_tool_handoff = original_system_prompt
    agent._original_max_completion_tokens_before_text_tool_handoff = original_max_tokens
    agent._system_prompt = system_prompt
    agent._max_completion_tokens_for_route = max_completion_tokens_for_route
    agent._text_tool_handoff_installed = True
