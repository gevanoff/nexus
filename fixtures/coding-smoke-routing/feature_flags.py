from __future__ import annotations

from typing import Mapping


def enabled_features(settings: Mapping[str, object]) -> list[str]:
    """Return sorted names for enabled feature flags."""
    enabled: list[str] = []
    for name, raw_value in settings.items():
        if raw_value is True:
            enabled.append(str(name))
    return sorted(enabled)
