from __future__ import annotations

from pathlib import Path


path = Path("services/gateway/app/coding_agent.py")
text = path.read_text(encoding="utf-8")
old = '''def _resolved_alias_for_route(
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
'''
new = '''def _resolved_alias_for_route(
    model: str,
    backend: str,
    upstream_model: str,
) -> Any:
    aliases = get_aliases()
    requested = aliases.get(str(model or "").strip().lower())
    if not backend and not upstream_model:
        return requested

    registry = get_registry()
    resolver = getattr(registry, "resolve_backend_class", None)

    def resolve_backend(value: str) -> str:
        if callable(resolver):
            return resolver(value) or value
        return value

    resolved_backend = resolve_backend(backend)
    normalized_upstream = str(upstream_model or "").strip().lower()

    def matches(alias: Any) -> bool:
        if alias is None:
            return False
        if resolve_backend(alias.backend) != resolved_backend:
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
'''
count = text.count(old)
if count != 1:
    raise RuntimeError(f"resolved alias compatibility anchor: expected 1, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
