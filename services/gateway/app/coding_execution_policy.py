from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ExecutionPolicySnapshot:
    backend: str
    upstream_model: str
    text_tool_mode: bool
    forced_state_key: str
    action_kind: str
    allowed_tools: tuple[str, ...]
    plan_revision: int
    signature: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tool_name(spec: Any) -> str:
    function = getattr(spec, "function", None)
    if function is None and isinstance(spec, Mapping):
        function = spec.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(getattr(function, "name", "") or "").strip()


def capture(
    agent: Any,
    task: Mapping[str, Any],
    *,
    backend: str,
    upstream_model: str,
) -> ExecutionPolicySnapshot:
    forced = agent.forced_action.active_state(task)
    specs: Sequence[Any] = agent._tool_specs_for_task(dict(task))
    allowed_tools = tuple(
        sorted(name for name in (_tool_name(spec) for spec in specs) if name)
    )
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), Mapping) else {}
    try:
        plan_revision = max(0, int(plan.get("revision") or 0))
    except (TypeError, ValueError):
        plan_revision = 0
    payload = {
        "backend": str(backend or "").strip(),
        "upstream_model": str(upstream_model or "").strip(),
        "text_tool_mode": not agent._backend_supports_tool_calling(backend),
        "forced_state_key": str(forced.get("state_key") or ""),
        "action_kind": str(forced.get("action_kind") or ""),
        "allowed_tools": list(allowed_tools),
        "plan_revision": plan_revision,
    }
    signature = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ExecutionPolicySnapshot(
        backend=payload["backend"],
        upstream_model=payload["upstream_model"],
        text_tool_mode=bool(payload["text_tool_mode"]),
        forced_state_key=payload["forced_state_key"],
        action_kind=payload["action_kind"],
        allowed_tools=allowed_tools,
        plan_revision=plan_revision,
        signature=signature,
    )
