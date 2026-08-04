from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


agent_path = Path("services/gateway/app/coding_agent.py")
text = agent_path.read_text(encoding="utf-8")

old = '''def _max_completion_tokens_for_route(model: str, backend: str) -> int:
    cap = _max_completion_tokens()
    alias = get_aliases().get(str(model or "").strip().lower())
    if alias is not None and alias.max_tokens_cap is not None:
        try:
            cap = max(128, min(int(alias.max_tokens_cap), 32_768))
        except Exception:
            pass
    if not _backend_supports_tool_calling(backend):
        cap = min(cap, _text_tool_max_completion_tokens())
    return cap
'''
new = '''def _resolved_alias_for_route(
    model: str,
    backend: str,
    upstream_model: str,
) -> Any:
    aliases = get_aliases()
    requested = aliases.get(str(model or "").strip().lower())
    registry = get_registry()
    resolved_backend = registry.resolve_backend_class(backend) or backend
    normalized_upstream = str(upstream_model or "").strip().lower()

    def matches(alias: Any) -> bool:
        if alias is None:
            return False
        alias_backend = registry.resolve_backend_class(alias.backend) or alias.backend
        if alias_backend != resolved_backend:
            return False
        if not normalized_upstream:
            return alias is requested
        return str(alias.upstream_model or "").strip().lower() == normalized_upstream

    if matches(requested):
        return requested

    matching_coding_aliases = [
        alias
        for alias in aliases.values()
        if alias is not requested and alias.coding is True and matches(alias)
    ]
    if not matching_coding_aliases:
        return None
    return min(
        matching_coding_aliases,
        key=lambda alias: int(alias.context_window or 2**31),
    )


def _max_completion_tokens_for_route(
    model: str,
    backend: str,
    upstream_model: str = "",
) -> int:
    cap = _max_completion_tokens()
    alias = _resolved_alias_for_route(model, backend, upstream_model)
    if alias is not None and alias.max_tokens_cap is not None:
        try:
            cap = max(128, min(int(alias.max_tokens_cap), 32_768))
        except Exception:
            pass
    if not _backend_supports_tool_calling(backend):
        cap = min(cap, _text_tool_max_completion_tokens())
    return cap
'''
text = replace_once(text, old, new, "resolved route completion policy")

old = '''def _context_reset_tokens(value: Optional[int] = None, *, model: str = "") -> int:
    alias = get_aliases().get(str(model or "").strip().lower())
    alias_limit = getattr(alias, "coding_context_reset_tokens", None) if alias is not None else None
    if isinstance(alias_limit, int) and alias_limit > 0:
        return max(8_000, alias_limit)
    return max(8_000, context_budget.estimate_char_budget_tokens(_context_reset_chars(value)))
'''
new = '''def _context_reset_tokens(
    value: Optional[int] = None,
    *,
    model: str = "",
    backend: str = "",
    upstream_model: str = "",
) -> int:
    alias = _resolved_alias_for_route(model, backend, upstream_model)
    alias_limit = getattr(alias, "coding_context_reset_tokens", None) if alias is not None else None
    if isinstance(alias_limit, int) and alias_limit > 0:
        return max(8_000, alias_limit)
    context_window = getattr(alias, "context_window", None) if alias is not None else None
    if isinstance(context_window, int) and context_window > 0:
        return max(8_000, int(context_window * 0.8))
    return max(8_000, context_budget.estimate_char_budget_tokens(_context_reset_chars(value)))
'''
text = replace_once(text, old, new, "resolved route reset policy")

old = '''        context_reset_tokens = _context_reset_tokens(
            context_policy.get("context_reset_chars"),
            model=model,
        )
'''
new = '''        context_reset_tokens = _context_reset_tokens(
            context_policy.get("context_reset_chars"),
            model=model,
            backend=backend,
            upstream_model=upstream_model,
        )
'''
text = replace_once(text, old, new, "initial resolved reset budget")

old = '''            context_chars = _messages_char_count(messages)
            request_text_tool_mode = not _backend_supports_tool_calling(backend)
            context_tokens = _messages_token_count(
                messages,
                tools=None if request_text_tool_mode else tools,
            )
            completion_reserve_tokens = _max_completion_tokens_for_route(model, backend)
            reset_for_cycles = context_reset_cycles > 0 and cycle > 1 and (cycle - 1) % context_reset_cycles == 0
'''
new = '''            context_chars = _messages_char_count(messages)
            request_text_tool_mode = not _backend_supports_tool_calling(backend)
            context_tokens = _messages_token_count(
                messages,
                tools=None if request_text_tool_mode else tools,
            )
            resolved_context_reset_tokens = _context_reset_tokens(
                context_policy.get("context_reset_chars"),
                model=model,
                backend=backend,
                upstream_model=upstream_model,
            )
            if resolved_context_reset_tokens != context_reset_tokens:
                context_reset_tokens = resolved_context_reset_tokens
                await asyncio.to_thread(
                    _mutate_task,
                    task_id,
                    {
                        "agent_context_reset_tokens": context_reset_tokens,
                        "agent_context_reset_route": {
                            "backend": backend,
                            "upstream_model": upstream_model,
                        },
                    },
                )
            reset_for_cycles = context_reset_cycles > 0 and cycle > 1 and (cycle - 1) % context_reset_cycles == 0
'''
text = replace_once(text, old, new, "reroute-aware reset budget")

text = replace_once(
    text,
    '                max_tokens=_max_completion_tokens_for_route(model, backend),\n',
    '                max_tokens=_max_completion_tokens_for_route(model, backend, upstream_model),\n',
    "resolved route request completion budget",
)

agent_path.write_text(text, encoding="utf-8")


tests_path = Path("services/gateway/tests/test_glm_context_profiles.py")
tests = tests_path.read_text(encoding="utf-8")
tests += '''


def test_coding_budgets_follow_resolved_model_instead_of_selector(monkeypatch):
    coder = model_aliases.ModelAlias(
        backend="local_mlx",
        upstream_model="mlx-community/GLM-5.2-4bit",
        context_window=131_072,
        coding_context_reset_tokens=90_000,
        max_tokens_cap=16_384,
        coding=True,
    )
    deepseek = model_aliases.ModelAlias(
        backend="local_mlx",
        upstream_model="mlx-community/DeepSeek-R1-0528-4bit",
        context_window=65_536,
        max_tokens_cap=2_048,
        coding=True,
    )

    class Registry:
        @staticmethod
        def resolve_backend_class(value):
            return value

    monkeypatch.setattr(
        coding_agent,
        "get_aliases",
        lambda: {"coder": coder, "deepseek-r1": deepseek},
    )
    monkeypatch.setattr(coding_agent, "get_registry", lambda: Registry())
    monkeypatch.setattr(coding_agent, "_backend_supports_tool_calling", lambda _backend: True)

    assert coding_agent._context_reset_tokens(
        64_000,
        model="coder",
        backend="local_mlx",
        upstream_model=deepseek.upstream_model,
    ) == 52_428
    assert coding_agent._max_completion_tokens_for_route(
        "coder",
        "local_mlx",
        deepseek.upstream_model,
    ) == 2_048


def test_coding_budgets_keep_requested_alias_when_route_matches(monkeypatch):
    coder = _glm_alias()

    class Registry:
        @staticmethod
        def resolve_backend_class(value):
            return value

    monkeypatch.setattr(coding_agent, "get_aliases", lambda: {"coder": coder})
    monkeypatch.setattr(coding_agent, "get_registry", lambda: Registry())
    monkeypatch.setattr(coding_agent, "_backend_supports_tool_calling", lambda _backend: True)

    assert coding_agent._context_reset_tokens(
        64_000,
        model="coder",
        backend="local_mlx",
        upstream_model=coder.upstream_model,
    ) == 90_000
    assert coding_agent._max_completion_tokens_for_route(
        "coder",
        "local_mlx",
        coder.upstream_model,
    ) == 16_384
'''
tests_path.write_text(tests, encoding="utf-8")
