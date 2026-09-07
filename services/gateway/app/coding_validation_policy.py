from __future__ import annotations

from typing import Any, Mapping


def requires_agent_validation(task: Mapping[str, Any]) -> bool:
    """Return whether validation must run inside the author-agent session."""
    return str(task.get("kind") or "") != "harness_eval"
