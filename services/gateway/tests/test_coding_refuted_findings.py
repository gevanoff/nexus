from __future__ import annotations

from types import SimpleNamespace

from app import coding_completion_state_hardening as hardening
from app import coding_refuted_findings as refuted


NOTE = (
    "Root cause: old causal claim.\n"
    "Repository evidence: app.py:1-10\n"
    "Competing explanation checked: alternate.\n"
    "Expected result: repair."
)


def _lifecycle() -> dict:
    return {
        "schema": "nexus_coding_hypothesis_lifecycle.v1",
        "status": hardening._CONSUMED_STATUS,
        "plan_revision": 2,
        "note_fingerprint": hardening._note_fingerprint(NOTE),
        "consumed_at": 100.0,
    }


class _Resilience:
    def __init__(self):
        self.calls: list[tuple[dict, list[dict]]] = []
        self.build_working_memory = self._build

    def _build(self, task, *, state_key, controller, ledger, events):
        del state_key, controller, ledger
        previous = dict(task.get("agent_working_memory") or {})
        findings = list(previous.get("findings") or [])
        for event in events:
            if event.get("type") == "assistant" and event.get("content"):
                findings.append(str(event["content"]))
        result = {
            "findings": findings[-6:],
            "blocker": str(previous.get("blocker") or ""),
            "unresolved_question": str(previous.get("unresolved_question") or "fresh question"),
            "next_action": str(previous.get("next_action") or "fresh action"),
        }
        self.calls.append((dict(task), [dict(event) for event in events]))
        return result


def _task() -> dict:
    return {
        "project_plan": {
            "revision": 2,
            "note": NOTE,
            "updated_at": 80.0,
        },
        hardening._LIFECYCLE_KEY: _lifecycle(),
        "agent_working_memory": {
            "findings": ["old frontend conclusion", "old backend conclusion"],
            "blocker": "old blocker",
            "unresolved_question": "old question",
            "next_action": "old action",
        },
    }


def test_consuming_mutation_supersedes_prior_findings_and_blocker():
    resilience = _Resilience()
    refuted.install(resilience, hardening)
    task = _task()
    events = [
        {"type": "assistant", "ts": 90.0, "content": "pre-edit assistant claim"},
        {"type": "assistant", "ts": 110.0, "content": "post-edit assistant claim"},
    ]

    memory = resilience.build_working_memory(
        task,
        state_key="state",
        controller={},
        ledger=[],
        events=events,
    )

    assert memory["findings"] == [refuted._ACTIVE_MARKER]
    assert memory["blocker"] == ""
    assert memory["superseded_findings"] == [
        "old frontend conclusion",
        "old backend conclusion",
    ]
    assert memory["superseded_blockers"] == ["old blocker"]
    assert memory["superseded_reason"] == "hypothesis_consumed"
    assert resilience.calls[-1][1] == [
        {"type": "assistant", "ts": 110.0, "content": "post-edit assistant claim"}
    ]


def test_repeated_checkpoint_does_not_reimport_superseded_findings():
    resilience = _Resilience()
    refuted.install(resilience, hardening)
    task = _task()
    first = resilience.build_working_memory(
        task,
        state_key="state",
        controller={},
        ledger=[],
        events=[],
    )
    task["agent_working_memory"] = first

    second = resilience.build_working_memory(
        task,
        state_key="state",
        controller={},
        ledger=[],
        events=[
            {"type": "assistant", "ts": 120.0, "content": "another unverified claim"}
        ],
    )

    assert second["findings"] == [refuted._ACTIVE_MARKER]
    assert second["superseded_findings"] == [
        "old frontend conclusion",
        "old backend conclusion",
    ]


def test_fresh_plan_revision_starts_new_findings_epoch_after_plan_update():
    resilience = _Resilience()
    refuted.install(resilience, hardening)
    task = _task()
    consumed_memory = resilience.build_working_memory(
        task,
        state_key="state",
        controller={},
        ledger=[],
        events=[],
    )
    task["agent_working_memory"] = consumed_memory
    task["project_plan"] = {
        "revision": 3,
        "note": NOTE + "\nFresh evidence: config.py:20-30",
        "updated_at": 150.0,
    }

    memory = resilience.build_working_memory(
        task,
        state_key="state-2",
        controller={},
        ledger=[],
        events=[
            {"type": "assistant", "ts": 120.0, "content": "between-edit stale claim"},
            {"type": "assistant", "ts": 160.0, "content": "post-revalidation finding"},
        ],
    )

    assert refuted._ACTIVE_MARKER not in memory["findings"]
    assert "between-edit stale claim" not in memory["findings"]
    assert "post-revalidation finding" in memory["findings"]
    assert memory["superseded_findings"] == [
        "old frontend conclusion",
        "old backend conclusion",
    ]


def test_install_is_idempotent():
    resilience = _Resilience()
    original = resilience.build_working_memory
    refuted.install(resilience, hardening)
    installed = resilience.build_working_memory
    refuted.install(resilience, hardening)

    assert resilience.build_working_memory is installed
    assert resilience._build_working_memory_before_refuted_findings is original
