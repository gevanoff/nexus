from __future__ import annotations

import asyncio
import json
import threading

import pytest

from app import coding_agent_guarded as guarded


_RAW_SEMANTIC_ACCEPTANCE_REVIEW = guarded._semantic_acceptance_review


@pytest.fixture(autouse=True)
def _exercise_guarded_dispatcher_before_mission_epoch(monkeypatch):
    base = getattr(
        guarded,
        "_run_tool_with_semantic_acceptance_before_mission_acceptance_epoch",
        guarded._run_tool_with_semantic_acceptance,
    )
    monkeypatch.setattr(guarded, "_run_tool_with_semantic_acceptance", base)
    monkeypatch.setattr(
        guarded,
        "_semantic_acceptance_review",
        _RAW_SEMANTIC_ACCEPTANCE_REVIEW,
    )


def test_run_command_snapshots_baseline_and_marks_workspace_mutation(monkeypatch) -> None:
    task = {"agent_run_id": "run-1", "agent_events": []}
    fingerprints = iter(["before", "after"])
    baselines: list[str] = []

    monkeypatch.setattr(guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(
        guarded.cw,
        "workspace_progress_fingerprint",
        lambda _task_id: next(fingerprints),
    )
    monkeypatch.setattr(
        guarded.coding_run_delta,
        "ensure_baseline",
        lambda _cw, task_id, _task: baselines.append(task_id) or {},
    )
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, _name, _args, *, git_token_value: {"ok": True},
    )

    result = guarded._run_tool_with_semantic_acceptance(
        "code_test",
        "coding_run_command",
        {"argv": ["python", "-c", "open('x.py','w').write('x')"]},
        git_token_value=None,
    )

    assert baselines == ["code_test"]
    assert result["workspace_modified"] is True


def test_mutation_is_blocked_when_semantic_baseline_is_unavailable(monkeypatch) -> None:
    task = {"agent_run_id": "run-1", "agent_events": []}
    tool_calls: list[str] = []
    monkeypatch.setattr(guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(
        guarded.coding_run_delta,
        "ensure_baseline",
        lambda *_args, **_kwargs: {"error": "transient git failure"},
    )
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, name, _args, *, git_token_value: tool_calls.append(name) or {"ok": True},
    )

    result = guarded._run_tool_with_semantic_acceptance(
        "code_test",
        "coding_replace_text",
        {"path": "x.py", "old_text": "x", "new_text": "y"},
        git_token_value=None,
    )

    assert result["ok"] is False
    assert result["error"] == "semantic_baseline_unavailable"
    assert tool_calls == []


def test_run_command_mutation_participates_in_existing_edit_gates() -> None:
    assert guarded._tool_result_modified_workspace(
        "coding_run_command",
        {"argv": ["python", "script.py"]},
        {"ok": False, "workspace_modified": True},
    ) is True


def test_finish_semantic_review_is_driven_by_actual_run_delta(monkeypatch) -> None:
    task = {
        "agent_run_id": "run-1",
        "agent_cycle": 4,
        "agent_events": [],
        "prompt": "Fix the issue",
    }
    reviews: list[str] = []

    monkeypatch.setattr(guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, _name, _args, *, git_token_value: {
            "ok": True,
            "success": True,
            "summary": "done",
        },
    )
    monkeypatch.setattr(
        guarded,
        "_run_delta_diff",
        lambda _task_id, _task: "diff --git a/x.py b/x.py\n+fixed\n",
    )
    monkeypatch.setattr(guarded, "_deterministic_acceptance_ready", lambda _task_id: True)

    async def fake_review(_task_id, _task, *, diff_text):
        reviews.append(diff_text)
        return {
            "accepted": True,
            "reason": "aligned",
            "causal_alignment": True,
            "existing_mechanism_checked": True,
            "acceptance_criteria_checked": True,
        }

    monkeypatch.setattr(guarded, "_semantic_acceptance_review", fake_review)
    monkeypatch.setattr(guarded._agent, "_append_event", lambda *_args, **_kwargs: None)

    result = guarded._run_tool_with_semantic_acceptance(
        "code_test",
        "coding_finish",
        {"success": True, "summary": "done"},
        git_token_value=None,
    )

    assert result["success"] is True
    assert reviews == ["diff --git a/x.py b/x.py\n+fixed\n"]


def test_retryable_semantic_review_failure_does_not_instruct_repository_repair(monkeypatch) -> None:
    task = {
        "agent_run_id": "run-1",
        "agent_cycle": 5,
        "agent_events": [],
        "prompt": "Fix the issue",
    }
    events: list[dict] = []

    monkeypatch.setattr(guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, _name, _args, *, git_token_value: {
            "ok": True,
            "success": True,
            "summary": "done",
        },
    )
    monkeypatch.setattr(
        guarded,
        "_run_delta_diff",
        lambda _task_id, _task: "diff --git a/x.py b/x.py\n+fixed\n",
    )
    monkeypatch.setattr(guarded, "_deterministic_acceptance_ready", lambda _task_id: True)

    async def fake_review(_task_id, _task, *, diff_text):
        assert diff_text
        return {
            "accepted": False,
            "reason": "reviewer response could not be parsed",
            "causal_alignment": False,
            "existing_mechanism_checked": False,
            "acceptance_criteria_checked": False,
            "review_error": True,
            "fingerprint": "review-fingerprint",
        }

    monkeypatch.setattr(guarded, "_semantic_acceptance_review", fake_review)
    monkeypatch.setattr(
        guarded._agent,
        "_append_event",
        lambda _task_id, event: events.append(dict(event)),
    )

    result = guarded._run_tool_with_semantic_acceptance(
        "code_test",
        "coding_finish",
        {"success": True, "summary": "done"},
        git_token_value=None,
    )

    assert result["success"] is False
    assert result["error"] == "semantic_reviewer_unavailable"
    assert result["interrupted"] is True
    assert result["resumable"] is True
    assert "repository repair is not required" in result["summary"]
    assert events[-1]["type"] == "semantic_acceptance_review"
    assert events[-1]["review_error"] is True
    assert events[-1]["fingerprint"] == "review-fingerprint"


def test_concurrent_same_cycle_finishes_keep_review_metadata_isolated(monkeypatch) -> None:
    task = {
        "agent_run_id": "run-1",
        "agent_cycle": 9,
        "agent_events": [],
        "prompt": "Fix the issue",
    }
    events: list[dict] = []
    results: dict[str, dict] = {}
    event_lock = threading.Lock()
    barrier = threading.Barrier(2)

    monkeypatch.setattr(guarded.cw, "load_task", lambda _task_id: dict(task))
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, _name, _args, *, git_token_value: {
            "ok": True,
            "success": True,
            "summary": "done",
        },
    )
    monkeypatch.setattr(
        guarded,
        "_run_delta_diff",
        lambda _task_id, _task: f"diff-{threading.current_thread().name}",
    )
    monkeypatch.setattr(guarded, "_deterministic_acceptance_ready", lambda _task_id: True)

    async def fake_review(_task_id, _task, *, diff_text):
        thread_name = threading.current_thread().name
        assert diff_text == f"diff-{thread_name}"
        barrier.wait(timeout=5)
        review_error = thread_name == "finish-error"
        return {
            "accepted": not review_error,
            "reason": "retry" if review_error else "aligned",
            "causal_alignment": not review_error,
            "existing_mechanism_checked": not review_error,
            "acceptance_criteria_checked": not review_error,
            "review_error": review_error,
            "fingerprint": f"fp-{thread_name}",
        }

    def append_event(_task_id, event):
        with event_lock:
            events.append(dict(event))

    monkeypatch.setattr(guarded, "_semantic_acceptance_review", fake_review)
    monkeypatch.setattr(guarded._agent, "_append_event", append_event)

    def finish() -> None:
        result = guarded._run_tool_with_semantic_acceptance(
            "code_test",
            "coding_finish",
            {"success": True, "summary": "done"},
            git_token_value=None,
        )
        with event_lock:
            results[threading.current_thread().name] = dict(result)

    threads = [
        threading.Thread(target=finish, name="finish-ok"),
        threading.Thread(target=finish, name="finish-error"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    by_fingerprint = {event["fingerprint"]: event for event in events}
    assert by_fingerprint["fp-finish-ok"]["review_error"] is False
    assert by_fingerprint["fp-finish-ok"]["accepted"] is True
    assert by_fingerprint["fp-finish-error"]["review_error"] is True
    assert by_fingerprint["fp-finish-error"]["accepted"] is False
    assert results["finish-ok"]["success"] is True
    assert results["finish-error"]["error"] == "semantic_reviewer_unavailable"


def test_unparseable_reviewer_response_fails_over_without_author_retry(monkeypatch) -> None:
    task = {"agent_model": "coder", "agent_backend": "author", "agent_upstream_model": "author-model"}
    routes = iter([
        {"backend": "local_mlx", "upstream_model": "a"},
        {"backend": "review-b", "upstream_model": "b"},
        None,
    ])
    calls: list[tuple[str, str, object]] = []

    monkeypatch.setattr(guarded._agent.user_llm, "is_user_model_id", lambda _model: False)
    monkeypatch.setattr(guarded._agent, "_semantic_reroute_candidate", lambda *_args, **_kwargs: next(routes))
    monkeypatch.setattr(guarded._agent, "_max_completion_tokens_for_route", lambda *_args: 1200)
    monkeypatch.setattr(guarded._agent, "_settings_for_task_owner", lambda _task: None)
    monkeypatch.setattr(guarded._agent, "_extract_assistant_message", lambda response: type("Message", (), {"content": response})())

    async def fake_chat(req, backend, upstream_model, **_kwargs):
        calls.append((backend, upstream_model, req.response_format))
        content = "not JSON" if backend == "local_mlx" else json.dumps({
            "accepted": True, "reason": "aligned", "causal_alignment": True,
            "existing_mechanism_checked": True, "acceptance_criteria_checked": True,
        })
        return content, backend, upstream_model

    monkeypatch.setattr(guarded, "_call_backend_chat_with_failover", fake_chat)
    review = asyncio.run(guarded._semantic_acceptance_review("code_test", task, diff_text="+ fixed"))

    assert review["accepted"] is True, review
    assert [call[0] for call in calls] == ["local_mlx", "review-b"]
    assert calls[0][2] == {"type": "json_object"}


def test_valid_semantic_rejection_does_not_fail_over(monkeypatch) -> None:
    task = {"agent_model": "coder", "agent_backend": "author", "agent_upstream_model": "author-model"}
    calls: list[str] = []
    monkeypatch.setattr(guarded._agent.user_llm, "is_user_model_id", lambda _model: False)
    monkeypatch.setattr(guarded._agent, "_semantic_reroute_candidate", lambda *_args, **_kwargs: {"backend": "review-a", "upstream_model": "a"})
    monkeypatch.setattr(guarded._agent, "_max_completion_tokens_for_route", lambda *_args: 1200)
    monkeypatch.setattr(guarded._agent, "_settings_for_task_owner", lambda _task: None)
    monkeypatch.setattr(guarded._agent, "_extract_assistant_message", lambda response: type("Message", (), {"content": response})())

    async def fake_chat(_req, backend, upstream_model, **_kwargs):
        calls.append(backend)
        return json.dumps({
            "accepted": False, "reason": "criterion missing", "causal_alignment": False,
            "existing_mechanism_checked": True, "acceptance_criteria_checked": True,
        }), backend, upstream_model

    monkeypatch.setattr(guarded, "_call_backend_chat_with_failover", fake_chat)
    review = asyncio.run(guarded._semantic_acceptance_review("code_test", task, diff_text="+ fixed"))

    assert review["accepted"] is False
    assert review.get("review_error") is not True
    assert calls == ["review-a"]


def test_finish_fails_closed_when_recorded_command_mutation_loses_delta(monkeypatch) -> None:
    task = {
        "agent_run_id": "run-1",
        "agent_events": [
            {"type": "started", "run_id": "run-1"},
            {
                "type": "tool_finished",
                "name": "coding_run_command",
                "result": {"ok": True, "workspace_modified": True},
            },
        ],
    }
    monkeypatch.setattr(guarded.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, _name, _args, *, git_token_value: {
            "ok": True,
            "success": True,
        },
    )
    monkeypatch.setattr(guarded, "_run_delta_diff", lambda _task_id, _task: "")

    result = guarded._run_tool_with_semantic_acceptance(
        "code_test",
        "coding_finish",
        {"success": True},
        git_token_value=None,
    )

    assert result["success"] is False
    assert result["error"] == "semantic_acceptance_missing_diff"


def test_rejected_finish_result_is_returned_without_semantic_review(monkeypatch) -> None:
    rejection = {"ok": False, "error": "forced_action_tool_rejected", "message": "finish rejected"}
    monkeypatch.setattr(
        guarded,
        "_ORIGINAL_RUN_TOOL",
        lambda _task_id, _name, _args, *, git_token_value: dict(rejection),
    )
    monkeypatch.setattr(
        guarded,
        "_run_delta_diff",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("semantic review must not start")),
    )

    result = guarded._run_tool_with_semantic_acceptance(
        "code_test",
        "coding_finish",
        {"success": True},
        git_token_value=None,
    )

    assert result == rejection
