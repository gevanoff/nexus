from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_agent as ca


def test_no_change_audit_fails_finish_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="Completed requested work.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is False
    assert "called coding_finish" in summary
    assert event is not None
    assert event["type"] == "no_change_audit"


def test_no_change_audit_fails_turn_limit_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=False,
        finish_success=False,
        finish_summary="Turn limit reached before the agent called coding_finish.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is False
    assert "No-change audit" in summary
    assert event is not None
    assert event["type"] == "no_change_audit"


def test_no_change_audit_preserves_runs_with_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="Completed requested work.",
        committed_changes=False,
        uncommitted_changes=True,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is True
    assert summary == "Completed requested work."
    assert event is None


def test_fix_oriented_request_is_marked_edit_expected():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Debug this failing workflow and fix the root cause in the repo.",
    }

    assert ca._request_expects_workspace_edits(task) is True
    prompt = ca._system_prompt(task)
    assert "This request is fix-oriented." in prompt
    assert "Do not stop at diagnosis alone" in prompt


def test_review_request_does_not_get_fix_oriented_prompt():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
    }

    assert ca._request_expects_workspace_edits(task) is False
    prompt = ca._system_prompt(task)
    assert "This request is fix-oriented." not in prompt


def test_max_turns_allows_up_to_ten_thousand():
    assert ca._max_turns() == 1000
    assert ca._max_turns(5000) == 5000
    assert ca._max_turns(20000) == 10000


def test_runtime_is_unlimited_by_default():
    assert ca._max_runtime_sec() is None


def test_explicit_runtime_can_still_be_used_when_requested():
    assert ca._max_runtime_sec(120) == 120


def test_model_is_reroutable_for_aliases_but_not_explicit_backend_model():
    assert ca._model_is_reroutable("coder") is True
    assert ca._model_is_reroutable("default") is True
    assert ca._model_is_reroutable("local_mlx:mlx-community/Qwen3-30B-A3B-4bit") is False


def test_rank_coding_backend_candidates_prefers_less_loaded_ready_host(monkeypatch):
    class FakeBackend:
        def __init__(self, base_url: str, limit: int):
            self.base_url = base_url
            self._limit = limit

        def supports(self, route_kind: str) -> bool:
            return route_kind == "chat"

        def get_limit(self, route_kind: str) -> int:
            return self._limit

    class FakeRegistry:
        def __init__(self):
            self.backends = {
                "local_mlx": FakeBackend("http://ai2:10240/v1", 2),
                "local_vllm": FakeBackend("http://ada2:8000/v1", 4),
                "local_vllm_fast": FakeBackend("http://ai1:8001/v1", 4),
            }

        def get_backend(self, backend_name: str):
            return self.backends.get(backend_name)

    class FakeChecker:
        def get_status(self, backend_name: str):
            return SimpleNamespace(error="")

        def is_ready(self, backend_name: str) -> bool:
            return backend_name != "local_mlx"

    class FakeAdmission:
        def get_stats(self):
            return {
                "local_mlx.chat": {"limit": 2, "available": 0, "inflight": 2},
                "local_vllm.chat": {"limit": 4, "available": 3, "inflight": 1},
                "local_vllm_fast.chat": {"limit": 4, "available": 4, "inflight": 0},
            }

    monkeypatch.setattr(ca, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(ca, "get_health_checker", lambda: FakeChecker())
    monkeypatch.setattr(ca, "get_admission_controller", lambda: FakeAdmission())
    monkeypatch.setattr(ca, "llm_backends", lambda: [("local_mlx", object()), ("local_vllm", object()), ("local_vllm_fast", object())])
    monkeypatch.setattr(ca, "backend_hostname", lambda backend_name, **kwargs: {"local_mlx": "ai2", "local_vllm": "ada2", "local_vllm_fast": "ai1"}[backend_name])
    monkeypatch.setattr(ca, "default_model_for_backend", lambda backend_name, cfg: f"model-for-{backend_name}")

    ranked = ca._rank_coding_backend_candidates("coder", "local_mlx", "model-for-local_mlx")

    assert [item["backend"] for item in ranked[:2]] == ["local_vllm_fast", "local_vllm"]
    assert ranked[-1]["backend"] == "local_mlx"
    assert ranked[-1]["ready"] is False