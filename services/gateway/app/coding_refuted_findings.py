from __future__ import annotations

from typing import Any, Mapping, Sequence


_SCHEMA = "nexus_coding_superseded_findings.v1"
_ACTIVE_MARKER = (
    "The previous remediation hypothesis has been consumed by a repository mutation; "
    "its earlier assistant-derived findings are superseded until fresh repository evidence "
    "supports a new project-plan revision."
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _consumed_lifecycle(task: Mapping[str, Any], hardening: Any) -> Mapping[str, Any]:
    lifecycle = _mapping(task.get(hardening._LIFECYCLE_KEY))
    return (
        lifecycle
        if str(lifecycle.get("status") or "") == hardening._CONSUMED_STATUS
        else {}
    )


def _epoch(lifecycle: Mapping[str, Any]) -> str:
    return ":".join(
        part
        for part in (
            str(lifecycle.get("note_fingerprint") or "").strip(),
            str(lifecycle.get("consumed_at") or "").strip(),
        )
        if part
    )


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _after_boundary(event: Mapping[str, Any], boundary_at: float) -> bool:
    return _as_float(event.get("ts")) > boundary_at


def _dedupe(values: Sequence[Any], *, limit: int = 24) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").strip().split())
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output[-max(1, int(limit)) :]


def install(resilience: Any, hardening: Any) -> None:
    """Supersede pre-mutation model findings instead of replaying them forever."""

    if bool(getattr(resilience, "_refuted_findings_installed", False)):
        return

    original_build = resilience.build_working_memory

    def build_working_memory(
        task: Mapping[str, Any],
        *,
        state_key: str,
        controller: Mapping[str, Any],
        ledger: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        lifecycle = _consumed_lifecycle(task, hardening)
        if not lifecycle:
            return original_build(
                task,
                state_key=state_key,
                controller=controller,
                ledger=ledger,
                events=events,
            )

        epoch = _epoch(lifecycle)
        previous = _mapping(task.get("agent_working_memory"))
        prior_epoch = str(previous.get("findings_epoch") or "")
        consumed_at = _as_float(lifecycle.get("consumed_at"))
        same_consumed_note = bool(hardening._matching_consumed_lifecycle(task))
        plan = _mapping(task.get("project_plan"))
        plan_updated_at = _as_float(plan.get("updated_at"))
        event_boundary = consumed_at
        if not same_consumed_note and plan_updated_at > consumed_at:
            # A fresh plan revision is the revalidation epoch. Do not resurrect
            # assistant notes produced after the old edit but before that plan.
            event_boundary = plan_updated_at

        historical_findings = list(previous.get("superseded_findings") or [])
        historical_blockers = list(previous.get("superseded_blockers") or [])
        sanitized_previous = dict(previous)
        if prior_epoch != epoch:
            historical_findings.extend(previous.get("findings") or [])
            blocker = str(previous.get("blocker") or "").strip()
            if blocker:
                historical_blockers.append(blocker)
            sanitized_previous["findings"] = []
            sanitized_previous["blocker"] = ""
            sanitized_previous["unresolved_question"] = ""
            sanitized_previous["next_action"] = ""
        elif not same_consumed_note:
            # The lifecycle marker is useful while a hypothesis is consumed but
            # must not become a permanent finding after a fresh plan revision.
            sanitized_previous["findings"] = [
                value
                for value in (sanitized_previous.get("findings") or [])
                if str(value or "").strip() != _ACTIVE_MARKER
            ]

        sanitized_task = dict(task)
        sanitized_task["agent_working_memory"] = sanitized_previous
        post_boundary_events = [
            event
            for event in events
            if isinstance(event, Mapping) and _after_boundary(event, event_boundary)
        ]
        memory = original_build(
            sanitized_task,
            state_key=state_key,
            controller=controller,
            ledger=ledger,
            events=post_boundary_events,
        )
        memory = dict(memory)
        memory["supersession_schema"] = _SCHEMA
        memory["findings_epoch"] = epoch
        memory["superseded_findings"] = _dedupe(historical_findings)
        memory["superseded_blockers"] = _dedupe(historical_blockers, limit=12)
        memory["superseded_at"] = consumed_at
        memory["superseded_reason"] = "hypothesis_consumed"

        # While the same consumed note is still the current plan revision, do not
        # promote new assistant prose into established findings. A fresh plan
        # revision is the explicit boundary that allows current findings again.
        if same_consumed_note:
            memory["findings"] = [_ACTIVE_MARKER]
            memory["blocker"] = ""
        return memory

    resilience.build_working_memory = build_working_memory
    resilience._build_working_memory_before_refuted_findings = original_build
    resilience._refuted_findings_installed = True
