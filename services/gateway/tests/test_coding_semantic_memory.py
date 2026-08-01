from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import coding_agent
from app import coding_semantic_memory as memory


def _task(*, stagnant_cycles=4, plan_revision=1, workspace_fingerprint="same"):
    return {
        "id": "code_abcdef123456",
        "prompt": "Fix archived workspace diagnostics",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "branch_name": "agent/example",
        "agent_status": "running",
        "agent_run_id": "run-2",
        "agent_cycle": 4,
        "agent_progress_state": {
            "stagnant_cycles": stagnant_cycles,
            "observation": {
                "workspace_fingerprint": workspace_fingerprint,
                "plan_revision": plan_revision,
                "validation_revision": 0,
                "diff_review_revision": 0,
                "finish_state": "running",
            },
        },
        "mission": {"budget_policy": {"max_no_progress_cycles": 8}},
        "project_plan": {
            "goal": "Fix archived workspace diagnostics",
            "revision": plan_revision,
            "items": [
                {
                    "id": "inspect",
                    "title": "Trace archive diagnostics",
                    "status": "in_progress",
                    "summary": "Identify the smallest integration point",
                }
            ],
        },
        "agent_events": [
            {"type": "started", "run_id": "run-1"},
            {
                "type": "tool_started",
                "name": "coding_read_file_lines",
                "args": {"path": "old.py", "start_line": 1, "line_count": 20},
            },
            {"type": "started", "run_id": "run-2"},
            {
                "type": "assistant",
                "content": "The archive report lacks a stable stop-reason summary; I need the rendering call site.",
            },
            {
                "type": "tool_started",
                "name": "coding_search_text",
                "args": {"path": "services/gateway/app", "query": "inspect_archived_task"},
            },
            {
                "type": "tool_started",
                "name": "coding_read_file_lines",
                "args": {
                    "path": "services/gateway/app/coding_workspace.py",
                    "start_line": 3043,
                    "line_count": 80,
                },
            },
        ],
    }


def _install_workspace_stubs(monkeypatch, task):
    messages = []

    def mutate(_task_id, mutator):
        before = len(task.get("guidance_messages") or [])
        mutator(task)
        for item in (task.get("guidance_messages") or [])[before:]:
            messages.append(
                {
                    "message": str(item.get("content") or ""),
                    "actor": str(item.get("actor") or ""),
                }
            )
        return task

    monkeypatch.setattr(memory.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(memory.cw, "mutate_task", mutate)
    monkeypatch.setattr(memory.cw, "normalize_coding_mission", lambda value: value["mission"])
    monkeypatch.setattr(memory.cw, "normalize_project_plan", lambda value, fallback_goal="": value)
    return messages


def test_checkpoint_contains_only_current_run_inspection(monkeypatch):
    task = _task()
    monkeypatch.setattr(memory.cw, "normalize_project_plan", lambda value, fallback_goal="": value)

    checkpoint = memory.build_investigation_checkpoint(task)

    assert checkpoint["inspected_targets"] == [
        "search services/gateway/app: inspect_archived_task",
        "read services/gateway/app/coding_workspace.py lines 3043-3122",
    ]
    assert "old.py" not in str(checkpoint)
    assert checkpoint["active_plan_item"].startswith("Trace archive diagnostics")
    assert checkpoint["unverified_model_notes"]


def test_stagnation_checkpoint_is_persisted_and_injected_once(monkeypatch):
    task = _task()
    messages = _install_workspace_stubs(monkeypatch, task)
    monkeypatch.setattr(
        memory.cw,
        "append_guidance_message",
        lambda *args, **kwargs: pytest.fail("checkpoint guidance must use the atomic task mutation"),
    )

    assert memory.process_task(task["id"]) is True
    assert memory.process_task(task["id"]) is False

    assert len(messages) == 1
    assert messages[0]["actor"] == "nexus-controller"
    assert "Required next action" in messages[0]["message"]
    assert task["agent_investigation_checkpoint"]["stagnant_cycles"] == 4
    assert task["agent_events"][-1]["type"] == "investigation_checkpoint"


def test_checkpoint_guidance_survives_task_context_hydration(monkeypatch):
    task = _task()
    messages = _install_workspace_stubs(monkeypatch, task)
    assert memory.process_task(task["id"]) is True
    task.update(
        {
            "agent_previous_run_id": "run-1",
            "agent_previous_status": "paused",
            "agent_previous_summary": "Coding run paused after eight stagnant cycles.",
        }
    )
    task["agent_events"].extend(
        {"type": "cycle_started", "cycle": index} for index in range(20)
    )
    monkeypatch.setattr(coding_agent.cw, "coding_state_snapshot", lambda _task_id: {})

    context = coding_agent._task_context(task)

    assert len(messages) == 1
    assert "Controller investigation checkpoint" in context
    assert "Required next action" in context
    assert "inspect_archived_task" in context


def test_required_action_precedes_bounded_inspection_ledger(monkeypatch):
    task = _task()
    monkeypatch.setattr(memory.cw, "normalize_project_plan", lambda value, fallback_goal="": value)
    checkpoint = memory.build_investigation_checkpoint(task)

    guidance = memory.render_checkpoint_guidance(checkpoint)

    assert guidance.index("Required next action") < guidance.index("Already inspected")
    assert "Required next action" in guidance[:1600]


def test_durable_state_change_allows_one_new_checkpoint(monkeypatch):
    task = _task()
    messages = _install_workspace_stubs(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    task["agent_progress_state"]["observation"]["plan_revision"] = 2
    task["agent_progress_state"]["stagnant_cycles"] = 4
    task["project_plan"]["revision"] = 2

    assert memory.process_task(task["id"]) is True
    assert memory.process_task(task["id"]) is False
    assert len(messages) == 2


def test_new_run_same_durable_state_does_not_receive_fresh_credit(monkeypatch):
    task = _task()
    messages = _install_workspace_stubs(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 4
    task["agent_progress_state"]["stagnant_cycles"] = 4
    task["agent_events"].append({"type": "started", "run_id": "run-3"})

    assert memory.process_task(task["id"]) is False
    assert len(messages) == 1


def test_small_no_progress_budget_intervenes_before_terminal_cycle(monkeypatch):
    task = _task(stagnant_cycles=1)
    task["mission"]["budget_policy"]["max_no_progress_cycles"] = 2
    monkeypatch.setattr(memory.cw, "normalize_coding_mission", lambda value: value["mission"])

    assert memory._stagnation_threshold(task) == 1


def test_inactive_workspace_is_ignored(monkeypatch):
    task = _task()
    task["agent_status"] = "paused"
    messages = _install_workspace_stubs(monkeypatch, task)

    assert memory.process_task(task["id"]) is False
    assert messages == []


@pytest.mark.asyncio
async def test_cycle_boundary_checkpoint_recovers_before_terminal_pause(monkeypatch):
    calls = []
    decision = SimpleNamespace(
        pause=True,
        reason_code="no_progress_limit",
        summary="paused",
        state=SimpleNamespace(stagnant_cycles=8),
    )
    monkeypatch.setattr(
        coding_agent.coding_semantic_memory,
        "process_task",
        lambda task_id: calls.append(("checkpoint", task_id)) or True,
    )
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    await coding_agent._enforce_cycle_progress_decision(
        "code_abcdef123456",
        cycle=8,
        decision=decision,
    )

    assert calls == [
        ("checkpoint", "code_abcdef123456"),
        ("event", "no_progress_recovery"),
    ]


@pytest.mark.asyncio
async def test_cycle_boundary_recovers_when_background_scanner_won_claim(monkeypatch):
    calls = []
    decision = SimpleNamespace(
        pause=True,
        reason_code="no_progress_limit",
        summary="paused",
        state=SimpleNamespace(stagnant_cycles=8),
    )
    monkeypatch.setattr(
        coding_agent.coding_semantic_memory,
        "process_task",
        lambda task_id: calls.append(("checkpoint", task_id)) or False,
    )
    monkeypatch.setattr(
        coding_agent.cw,
        "load_task",
        lambda _task_id: {
            "agent_run_id": "run-2",
            "agent_investigation_checkpoint": {"run_id": "run-2", "cycle": 8},
        },
    )
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    await coding_agent._enforce_cycle_progress_decision(
        "code_abcdef123456",
        cycle=8,
        decision=decision,
    )

    assert calls == [
        ("checkpoint", "code_abcdef123456"),
        ("event", "no_progress_recovery"),
    ]


@pytest.mark.asyncio
async def test_cycle_boundary_pauses_after_checkpoint_credit_is_used(monkeypatch):
    calls = []
    decision = SimpleNamespace(
        pause=True,
        reason_code="no_progress_limit",
        summary="paused",
        state=SimpleNamespace(stagnant_cycles=8),
    )
    monkeypatch.setattr(
        coding_agent.coding_semantic_memory,
        "process_task",
        lambda task_id: calls.append(("checkpoint", task_id)) or False,
    )
    monkeypatch.setattr(
        coding_agent.cw,
        "load_task",
        lambda _task_id: {
            "agent_run_id": "run-2",
            "agent_investigation_checkpoint": {"run_id": "run-2", "cycle": 7},
        },
    )
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    with pytest.raises(coding_agent._CodingAgentPaused) as exc_info:
        await coding_agent._enforce_cycle_progress_decision(
            "code_abcdef123456",
            cycle=8,
            decision=decision,
        )

    assert exc_info.value.reason_code == "no_progress_limit"
    assert calls == [
        ("checkpoint", "code_abcdef123456"),
        ("event", "no_progress_limit"),
    ]


@pytest.mark.asyncio
async def test_cycle_boundary_records_checkpoint_failure_before_pausing(monkeypatch):
    calls = []
    decision = SimpleNamespace(
        pause=True,
        reason_code="no_progress_limit",
        summary="paused",
        state=SimpleNamespace(stagnant_cycles=8),
    )

    def fail_checkpoint(_task_id):
        calls.append(("checkpoint", "failed"))
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(coding_agent.coding_semantic_memory, "process_task", fail_checkpoint)
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    with pytest.raises(coding_agent._CodingAgentPaused):
        await coding_agent._enforce_cycle_progress_decision(
            "code_abcdef123456",
            cycle=8,
            decision=decision,
        )

    assert calls == [
        ("checkpoint", "failed"),
        ("event", "investigation_checkpoint_error"),
        ("event", "no_progress_limit"),
    ]


@pytest.mark.asyncio
async def test_runtime_start_and_stop_are_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        memory,
        "scan_once",
        lambda: calls.append("scan") or {"ok": True, "processed": [], "failures": {}},
    )
    monkeypatch.setattr(memory, "_poll_interval", lambda: 60.0)
    memory._RUNTIME_TASK = None

    await memory.start_runtime()
    first = memory._RUNTIME_TASK
    await memory.start_runtime()
    assert memory._RUNTIME_TASK is first
    for _ in range(20):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls == ["scan"]

    await memory.stop_runtime()
    assert memory._RUNTIME_TASK is None
    await memory.stop_runtime()
