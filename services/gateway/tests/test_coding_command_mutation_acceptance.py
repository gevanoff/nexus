from __future__ import annotations

from app import coding_agent_guarded as guarded


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
