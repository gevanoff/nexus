from __future__ import annotations

import re
from typing import Any


def _has_standalone_occurrence(text: str, name: str) -> bool:
    """Return true when a filename is cited independently, not inside a path/URL.

    Full paths are handled separately by coding_contract_hardening._repository_paths.
    Basename recovery exists only for genuinely bare citations such as
    ``config.py``. Without this boundary check, ``/etc/config.py`` and
    ``https://example.test/...`` could accidentally manufacture corrective
    basename targets from path/domain fragments.
    """
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    for match in pattern.finditer(str(text or "")):
        before = str(text or "")[match.start() - 1] if match.start() > 0 else ""
        after = str(text or "")[match.end()] if match.end() < len(str(text or "")) else ""
        if before in {"/", "\\"} or after in {"/", "\\"}:
            continue
        return True
    return False


def install(hardening: Any) -> None:
    """Install basename-boundary filtering on the Coding Workspace hardening layer."""
    if bool(getattr(hardening, "_coding_contract_path_safety_installed", False)):
        return

    original = hardening._repository_basenames

    def repository_basenames_with_boundaries(text: str) -> list[str]:
        return [
            name
            for name in original(text)
            if _has_standalone_occurrence(str(text or ""), str(name or ""))
        ]

    hardening._repository_basenames = repository_basenames_with_boundaries
    hardening._coding_contract_path_safety_installed = True
