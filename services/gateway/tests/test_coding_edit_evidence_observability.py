from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import coding_edit_evidence_continuity as continuity


class Dispatch:
    @staticmethod
    async def _record_policy_transition(agent, cw, task_id, *, task, snapshot, diagnostics, cycle):
        agent.original_record_calls.append(
            {
                "task_id": task_id,
                "cycle": cycle,
                "diagnostics": dict(diagnostics),
            }
        )


class Agent:
    def __init__(self) -> None:
        self.original_record_calls = []
        self.mutations = []
        self.events = []

    def _mutate_task(self, task_id, fields):
        self.mutations.append((task_id, dict(fields)))

    def _append_event(self, task_id, event):
        self.events.append((task_id, dict(event)))


def test_replay_observability_persists_safe_metadata_and_event():
    dispatch = Dispatch()
    agent = Agent()
    continuity._install_replay_observability(agent, dispatch)
    snapshot = SimpleNamespace(
        backend="local_vllm_fast",
        upstream_model="devstral",
        signature="policy-sig",
    )
    diagnostics = {
        "coding_request": True,
        "verified_evidence_replay_messages": 1,
        "verified_evidence_replay_phase": "edit",
        "verified_evidence_replay_role": "user",
        "verified_evidence_replay_chars": 4321,
        "verified_evidence_replay_source_chars": 5000,
        "verified_evidence_replay_paths": ["services/gateway/app/ui_routes.py"],
        "verified_evidence_replay_clipped_paths": ["services/gateway/app/ui_routes.py"],
        "verified_evidence_replay_path_stats": [
            {
                "path": "services/gateway/app/ui_routes.py",
                "source_chars": 5000,
                "replayed_chars": 4321,
                "clipped": True,
            }
        ],
    }

    asyncio.run(
        dispatch._record_policy_transition(
            agent,
            object(),
            "code-1",
            task={},
            snapshot=snapshot,
            diagnostics=diagnostics,
            cycle=11,
        )
    )

    assert len(agent.original_record_calls) == 1
    assert len(agent.mutations) == 1
    task_id, fields = agent.mutations[0]
    assert task_id == "code-1"
    replay = fields["agent_verified_evidence_replay"]
    assert replay["phase"] == "edit"
    assert replay["chars"] == 4321
    assert replay["source_chars"] == 5000
    assert replay["paths"] == ["services/gateway/app/ui_routes.py"]
    assert replay["clipped_paths"] == ["services/gateway/app/ui_routes.py"]
    assert replay["policy_signature"] == "policy-sig"

    assert len(agent.events) == 1
    event = agent.events[0][1]
    assert event["type"] == "verified_evidence_replay"
    assert event["cycle"] == 11
    assert "4321 chars" in event["summary"]
    assert "1 path(s)" in event["summary"]


def test_no_replay_observability_event_without_replay_message():
    dispatch = Dispatch()
    agent = Agent()
    continuity._install_replay_observability(agent, dispatch)
    snapshot = SimpleNamespace(backend="local_mlx", upstream_model="glm", signature="sig")

    asyncio.run(
        dispatch._record_policy_transition(
            agent,
            object(),
            "code-2",
            task={},
            snapshot=snapshot,
            diagnostics={"coding_request": True},
            cycle=4,
        )
    )

    assert len(agent.original_record_calls) == 1
    assert agent.mutations == []
    assert agent.events == []
