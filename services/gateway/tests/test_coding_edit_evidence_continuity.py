from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import coding_edit_evidence_continuity as continuity
from app.models import ChatMessage


TARGET = "services/gateway/app/ui_routes.py"


class Persistence:
    def __init__(self) -> None:
        self._verified_evidence_digest = lambda task, state: "legacy"

    @staticmethod
    def _normalized_path(value):
        return str(value or "").strip().replace("\\", "/").strip("/")

    @staticmethod
    def _verified_targets(state):
        return [str(item) for item in (state.get("causal_evidence_targets") or [])]

    @staticmethod
    def _successful_event_result(event):
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False or result.get("error"):
            return {}
        return result


class Forced:
    def __init__(self, state: dict) -> None:
        self.state = state

    def active_state(self, task):
        return dict(self.state)


class Dispatch:
    def __init__(self) -> None:
        self.coding_execution_policy = SimpleNamespace(
            execution_task=lambda agent, task: dict(task)
        )

    @staticmethod
    def _request_value(req, name, default=None):
        return req.get(name, default)

    @staticmethod
    def _copy_request(req, **updates):
        out = dict(req)
        out.update(updates)
        return out

    @staticmethod
    def materialize_request(agent, req, task, *, source_backend, backend, upstream_model):
        return (
            {**req, "messages": list(req.get("messages") or [])},
            SimpleNamespace(
                backend=backend,
                upstream_model=upstream_model,
                signature="sig",
            ),
            {"coding_request": True},
        )


def _task(source: str, *, edited: bool = False) -> dict:
    events = [
        {
            "type": "tool_finished",
            "name": "coding_read_file_lines",
            "ts": 15.0,
            "result": {"ok": True, "path": TARGET, "content": source},
        }
    ]
    if edited:
        events.append(
            {
                "type": "tool_finished",
                "name": "coding_apply_patch",
                "ts": 30.0,
                "result": {"ok": True, "changed": True},
            }
        )
    return {"id": "code-test", "agent_events": events}


def _edit_state() -> dict:
    return {
        "action_kind": "edit",
        "evidence_provenance_enforced": True,
        "hypothesis_causal_evidence_linked": True,
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
        "activated_at": 10.0,
        "durable_hypothesis_note_updated_at": 20.0,
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
    }


def _agent(state: dict):
    return SimpleNamespace(ChatMessage=ChatMessage, forced_action=Forced(state))


def test_edit_turn_replays_verified_source_after_hypothesis_contract_closes():
    source = (
        "if payload is None:\n"
        "    return entry\n\n"
        "models, management = _normalize_image_models_payload(payload)\n"
        "if backend_class == \"gpu_heavy\":\n"
        "    ui_url = _invokeai_ui_url()\n"
    )
    dispatch = Dispatch()
    persistence = Persistence()
    agent = _agent(_edit_state())
    continuity._install_materialization(agent, dispatch, persistence)
    req = {
        "messages": [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="mission"),
        ]
    }

    materialized, _, diagnostics = dispatch.materialize_request(
        agent,
        req,
        _task(source),
        source_backend="local_mlx",
        backend="local_vllm_fast",
        upstream_model="devstral",
    )

    assert len(materialized["messages"]) == 3
    evidence_message = materialized["messages"][-1]
    assert evidence_message.role == "user"
    assert source.strip() in evidence_message.content
    assert "Inspection tools are intentionally unavailable" in evidence_message.content
    assert "Do not request another read or search" in evidence_message.content
    assert diagnostics["verified_evidence_replay_phase"] == "edit"
    assert diagnostics["verified_evidence_replay_paths"] == [TARGET]
    assert diagnostics["verified_evidence_replay_clipped_paths"] == []
    assert diagnostics["verified_evidence_replay_chars"] >= len(source.strip())


def test_edit_turn_refreshes_replayed_evidence_from_current_checkpoint():
    stale_source = 'if backend_class == "gpu_heavy":\n    ui_url = _invokeai_ui_url()\n'
    current_source = (
        'if backend_class in ("gpu_heavy", "invokeai"):\n'
        '    ui_url = _invokeai_ui_url() or browser_accessible_url(base)\n'
    )

    class LiveWorkspace:
        def __init__(self) -> None:
            self.fingerprints = iter(["checkpoint-fingerprint", "checkpoint-fingerprint"])

        def workspace_progress_fingerprint(self, task_id):
            assert task_id == "code-test"
            return next(self.fingerprints)

        @staticmethod
        def git_head(task_id):
            assert task_id == "code-test"
            return {"ok": True, "commit": "checkpoint-head"}

        @staticmethod
        def read_file_lines(task_id, *, path, start_line, line_count):
            assert task_id == "code-test"
            assert path == TARGET
            assert start_line == 1
            assert line_count == 2
            return {
                "path": path,
                "start_line": 1,
                "end_line": 2,
                "content": current_source,
            }

    state = _edit_state()
    state["causal_evidence_ranges"] = [
        {"path": TARGET, "start_line": 1, "end_line": 2}
    ]
    dispatch = Dispatch()
    persistence = Persistence()
    agent = _agent(state)
    continuity._install_materialization(
        agent,
        dispatch,
        persistence,
        LiveWorkspace(),
    )

    task = _task(stale_source)
    task["last_commit"] = "checkpoint-head"

    materialized, _, diagnostics = dispatch.materialize_request(
        agent,
        {"messages": [ChatMessage(role="user", content="mission")]},
        task,
        source_backend="local_vllm_fast",
        backend="local_vllm_fast",
        upstream_model="devstral",
    )

    replay = materialized["messages"][-1].content
    assert current_source.strip() in replay
    assert stale_source.strip() not in replay
    assert diagnostics["verified_evidence_replay_source"] == "live_workspace"
    assert diagnostics["verified_evidence_replay_head"] == "checkpoint-head"
    assert (
        diagnostics["verified_evidence_replay_workspace_fingerprint"]
        == "checkpoint-fingerprint"
    )


def test_edit_turn_does_not_replay_evidence_across_a_workspace_change():
    class Paused(Exception):
        def __init__(self, message, *, reason_code, details):
            super().__init__(message)
            self.reason_code = reason_code
            self.details = details

    class ChangingWorkspace:
        def __init__(self) -> None:
            self.fingerprints = iter(["before", "after"])

        def workspace_progress_fingerprint(self, _task_id):
            return next(self.fingerprints)

        @staticmethod
        def git_head(_task_id):
            return {"ok": True, "commit": "checkpoint-head"}

        @staticmethod
        def read_file_lines(_task_id, *, path, start_line, line_count):
            return {
                "path": path,
                "start_line": start_line,
                "end_line": start_line + line_count - 1,
                "content": "current but raced\n",
            }

    state = _edit_state()
    state["causal_evidence_ranges"] = [
        {"path": TARGET, "start_line": 1, "end_line": 1}
    ]
    dispatch = Dispatch()
    persistence = Persistence()
    agent = _agent(state)
    agent._CodingAgentPaused = Paused
    continuity._install_materialization(
        agent,
        dispatch,
        persistence,
        ChangingWorkspace(),
    )

    with pytest.raises(Paused) as exc_info:
        dispatch.materialize_request(
            agent,
            {"messages": [ChatMessage(role="user", content="mission")]},
            _task("stale source\n"),
            source_backend="local_vllm_fast",
            backend="local_vllm_fast",
            upstream_model="devstral",
        )

    assert exc_info.value.reason_code == "verified_evidence_replay_unavailable"
    assert (
        exc_info.value.details["replay_status"]
        == "workspace_changed_during_refresh"
    )


def test_edit_turn_stops_replaying_after_successful_edit_tool_completion():
    dispatch = Dispatch()
    persistence = Persistence()
    agent = _agent(_edit_state())
    continuity._install_materialization(agent, dispatch, persistence)
    req = {
        "messages": [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="mission"),
        ]
    }

    materialized, _, diagnostics = dispatch.materialize_request(
        agent,
        req,
        _task("verified source", edited=True),
        source_backend="local_vllm_fast",
        backend="local_vllm_fast",
        upstream_model="devstral",
    )

    assert len(materialized["messages"]) == 2
    assert "verified_evidence_replay_phase" not in diagnostics


def test_small_verified_read_replays_losslessly():
    persistence = Persistence()
    source = "line 1\nline 2 causal branch\nline 3"
    digest, metadata = continuity.verified_evidence_bundle(
        persistence,
        _task(source),
        _edit_state(),
    )

    assert source in digest
    assert metadata == [
        {
            "path": TARGET,
            "source_chars": len(source),
            "replayed_chars": len(source),
            "clipped": False,
        }
    ]


def test_large_verified_read_preserves_head_middle_and_tail():
    lines = [f"line-{index:04d}-" + ("x" * 80) for index in range(180)]
    lines[0] = "HEAD_SENTINEL " + ("h" * 80)
    lines[len(lines) // 2] = "MIDDLE_SENTINEL " + ("m" * 80)
    lines[-1] = "TAIL_SENTINEL " + ("t" * 80)
    source = "\n".join(lines)

    clipped, was_clipped = continuity._line_aware_clip(source, 8_000)

    assert was_clipped is True
    assert len(clipped) <= 8_000
    assert "HEAD_SENTINEL" in clipped
    assert "MIDDLE_SENTINEL" in clipped
    assert "TAIL_SENTINEL" in clipped
    assert "omitted between head and middle" in clipped
    assert "omitted between middle and tail" in clipped
