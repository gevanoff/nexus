from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from app.model_aliases import get_aliases


_TOOL_CONTRACT_BUDGET_CHARS = 20_000
_TEXT_TOOL_COMPLETION_CEILING = 2_048
_TEXT_TOOL_COMPLETION_FALLBACK = 1_024


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
            "description": getattr(function, "description", ""),
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
        "description": str(raw.get("description") or "").strip(),
        "parameters": dict(parameters),
    }


def _serialize_contracts(specs: Sequence[Any]) -> str:
    contracts = [_function_payload(spec) for spec in specs]
    contracts = [item for item in contracts if item]
    contracts.sort(key=lambda item: item["name"])
    payload = json.dumps(contracts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(payload) <= _TOOL_CONTRACT_BUDGET_CHARS:
        return payload

    compact = []
    for item in contracts:
        compact.append(
            {
                "name": item["name"],
                "description": item["description"][:160],
                "parameters": item["parameters"],
            }
        )
    payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(payload) <= _TOOL_CONTRACT_BUDGET_CHARS:
        return payload

    # Parameter schemas are more important than prose descriptions for a text
    # backend that must emit executable workspace calls. Preserve every name and
    # schema before considering any truncation of the contract catalog.
    schema_only = [
        {"name": item["name"], "parameters": item["parameters"]}
        for item in contracts
    ]
    return json.dumps(schema_only, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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


def _matching_text_route_cap(backend: str, upstream_model: str) -> int:
    backend_key = str(backend or "").strip()
    model_key = str(upstream_model or "").strip().casefold()
    caps: list[int] = []
    for alias in get_aliases().values():
        if str(getattr(alias, "backend", "") or "").strip() != backend_key:
            continue
        alias_model = str(getattr(alias, "upstream_model", "") or "").strip().casefold()
        if model_key and alias_model != model_key:
            continue
        value = getattr(alias, "max_tokens_cap", None)
        if isinstance(value, int) and value > 0:
            caps.append(value)
    if not caps:
        return _TEXT_TOOL_COMPLETION_FALLBACK
    return min(_TEXT_TOOL_COMPLETION_CEILING, max(caps))


def install(agent: Any) -> None:
    """Make text-tool coding backends capable peers of native-tool routes.

    Coding Workspace keeps execution in the Gateway; vLLM models need only emit
    the same authorized workspace calls. This shim gives them exact current tool
    contracts and enough output budget to express edits/plan updates after an
    MLX failover, without enabling native tool transport on backends configured
    for text-form tool use.
    """
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
        if agent._backend_supports_tool_calling(backend):
            return original
        route_cap = _matching_text_route_cap(backend, upstream_model)
        return max(original, route_cap)

    agent._original_system_prompt_before_text_tool_handoff = original_system_prompt
    agent._original_max_completion_tokens_before_text_tool_handoff = original_max_tokens
    agent._system_prompt = system_prompt
    agent._max_completion_tokens_for_route = max_completion_tokens_for_route
    agent._text_tool_handoff_installed = True
