from __future__ import annotations

from typing import Any


def generate_preserving_conditioning(
    model: Any,
    text: str,
    kwargs: dict[str, Any],
    *,
    restore_after: bool,
) -> Any:
    """Run one generation and restore cached conditioning when a reference was used."""
    had_conds = hasattr(model, "conds")
    original_conds = getattr(model, "conds", None)
    try:
        return model.generate(text, **kwargs)
    finally:
        if restore_after:
            if had_conds:
                model.conds = original_conds
            elif hasattr(model, "conds"):
                delattr(model, "conds")
