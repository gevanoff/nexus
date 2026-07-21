from __future__ import annotations

from typing import Any

from app.backends import backend_provider_name
from app.model_aliases import ModelAlias, get_aliases


def capability_for_alias(name: str, alias: ModelAlias) -> dict[str, Any]:
    provider = backend_provider_name(alias.backend)
    supports = list(alias.supports_tool_choice or (("none", "auto", "required", "named") if alias.tools else ("none",)))
    return {
        "alias": name,
        "backend": alias.backend,
        "provider": provider,
        "upstream_model": alias.upstream_model,
        "tools_enabled": alias.tools is True,
        "tool_execution_mode": alias.tool_mode,
        "tool_execution_mode_explicit": alias.tool_mode_explicit,
        "tool_choice_modes": supports,
        "parallel_tool_calls": alias.supports_parallel_tool_calls,
        "buffered_tool_stream": alias.buffer_tool_call_stream,
        "tool_call_parser": alias.preferred_tool_call_parser or None,
        "chat_template": alias.preferred_chat_template or None,
        "reasoning_parser": alias.reasoning_parser or None,
        "strict_tools": alias.strict_tools,
        "auto_inject_tools": alias.auto_inject_tools,
        "toolsets": list(alias.toolsets),
        "max_tool_rounds": alias.max_tool_rounds,
    }


def tool_calling_diagnostics() -> list[dict[str, Any]]:
    return [capability_for_alias(name, alias) for name, alias in sorted(get_aliases().items())]
