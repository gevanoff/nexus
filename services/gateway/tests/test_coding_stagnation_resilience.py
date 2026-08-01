from __future__ import annotations

from app import coding_semantic_memory as memory
from app import coding_stagnation_resilience as resilience


def _task(*, stagnant_cycles: int = 4, plan_revision: int = 1, workspace: str = "same"):
    return {
        "id": "code_stagnation_test",
        "prompt": "Fix the repeated inspection loop",
        "agent_status": "running",
        "agent_run_id": "run-2",
        "agent_cycle": 4,
        "agent_progress_state": {
            "stagnant_cycles": stagnant_cycles,
            "observation": {
                "workspace_fingerprint": workspace,
                "plan_revision": plan_revision,
                "validation_revision": 0,
                "diff_review_revision": 0,
                "finish_state": "running",
                "guidance_revision": 0,
            },
        },
        "mission": {"budget_policy": {"max_no_progress_cycles": 8}},
        "project_plan": {
            "goal": "Fix the repeated inspection loop",
            "revision": plan_revision,
            "items": [
                {
                    "id": "inspect",
                    "title": "Trace the controller",
                    "status": "in_progress",
                    "summary": "Identify the smallest viable edit",
                }
            ],
        },
        "agent_events": [
            {"type": "started", "run_id": "run-2", "ts": 1},
            {
                "type": "assistant",
                "cycle": 1,
                "content": "The same controller path is being reconstructed.",
                "ts": 2,
            },
            {
                "type": "tool_started",
                "cycle": 2,
                "name": "coding_read_file_lines",
                "args": {"path": "services/gateway/app/coding_agent.py", "start_line": 100, "line_count": 40},
                "ts": 3,
            },
            {
                "type": "tool_started",
                "cycle": 3,
                "name": "coding_read_file_lines",
                "args": {"path": "services/gateway/app/coding_agent.py", "start_line": 140, "line_count": 40},
                "ts": 4,
            },
            {
                "type": "tool_started",
                "cycle": 4,
                "name": "coding_search_text",
                "args": {"path": "services/gateway/app", "query": "no progress controller"},
                "ts": 5,
            },
        ],
    }


def _install_workspace(monkeypatch, task):
    def mutate(_task_id, mutator):
        mutator(task)
        return task

    monkeypatch.setattr(memory.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(memory.cw, "mutate_task", mutate)
    monkeypatch.setattr(memory.cw, "normalize_coding_mission", lambda value: value["mission"])
    monkeypatch.setattr(memory.cw, "normalize_project_plan", lambda value, fallback_goal="": value)


def test_durable_state_key_ignores_plan_and_guidance_churn():
    task = _task()
    initial = resilience.durable_state_key(task)

    task["agent_progress_state"]["observation"]["plan_revision"] = 99
    task["agent_progress_state"]["observation"]["guidance_revision"] = 12345
    assert resilience.durable_state_key(task) == initial

    task["agent_progress_state"]["observation"]["workspace_fingerprint"] = "edited"
    assert resilience.durable_state_key(task) != initial


def test_adjacent_reads_share_one_semantic_inspection_signature():
    task = _task()
    events = resilience.current_run_events(task)
    ledger = resilience.update_inspection_ledger([], events, run_id="run-2", cycle=4)

    read_entries = [entry for entry in ledger if str(entry.get("signature") or "").startswith("read:")]
    assert len(read_entries) == 1
    assert read_entries[0]["count"] == 2
    assert read_entries[0]["target"].endswith("lines 140-179")


def test_controller_stagnation_survives_run_restart_for_same_state():
    task = _task(stagnant_cycles=7)
    key = resilience.durable_state_key(task)
    first = resilience.advance_controller(
        task,
        state_key=key,
        run_id="run-2",
        cycle=7,
        progress_stagnant_cycles=7,
        classification="inspection_loop",
        max_no_progress_cycles=8,
    )
    task["agent_stagnation_controller"] = first
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 1

    restarted = resilience.advance_controller(
        task,
        state_key=key,
        run_id="run-3",
        cycle=1,
        progress_stagnant_cycles=1,
        classification="inspection_loop",
        max_no_progress_cycles=8,
    )

    assert restarted["cycles"] == 8
    assert restarted["stage"] == "recovery"


def test_checkpoint_persists_working_memory_without_minting_progress(monkeypatch):
    task = _task()
    task["last_guidance_at"] = 111.0
    _install_workspace(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    assert memory.process_task(task["id"]) is False

    assert task["last_guidance_at"] == 111.0
    assert task["last_controller_guidance_at"] > 111.0
    assert task["agent_working_memory"]["schema"] == resilience.WORKING_MEMORY_SCHEMA
    assert task["agent_context_manifest"]["schema"] == resilience.CONTEXT_MANIFEST_SCHEMA
    assert task["agent_inspection_ledger"]
    assert task["agent_stagnation_controller"]["interventions"]


def test_one_plan_checkpoint_per_unchanged_output_state(monkeypatch):
    task = _task()
    _install_workspace(monkeypatch, task)
    assert memory.process_task(task["id"]) is True

    task["agent_progress_state"]["observation"]["plan_revision"] = 2
    task["project_plan"]["revision"] = 2
    assert memory.process_task(task["id"]) is True

    task["agent_progress_state"]["observation"]["plan_revision"] = 3
    task["project_plan"]["revision"] = 3
    assert memory.process_task(task["id"]) is False

    kinds = [item["kind"] for item in task["agent_stagnation_controller"]["interventions"]]
    assert kinds.count("plan_checkpoint") == 1


def test_no_progress_continuation_receives_one_state_keyed_recovery(monkeypatch):
    task = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_controller"] = {
        "schema": resilience.SCHEMA,
        "state_key": key,
        "run_id": "run-2",
        "last_cycle": 8,
        "cycles": 8,
        "plan_revision": 1,
        "processed_event_count": len(resilience.current_run_events(task)),
        "interventions": [
            {
                "id": resilience.intervention_id(key, "recovery"),
                "kind": "recovery",
                "run_id": "run-2",
                "cycle": 8,
            }
        ],
    }
    task["agent_previous_status"] = "paused"
    task["agent_previous_summary"] = "Coding run paused after 8 cycles without a durable state transition."
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 1
    task["agent_events"].append({"type": "started", "run_id": "run-3", "ts": 6})
    _install_workspace(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    assert task["agent_stagnation_recovery_lease"]["kind"] == "continuation"
    assert task["agent_stagnation_recovery_lease"]["remaining_transitions"] == 1
    assert memory.process_task(task["id"]) is False


def test_context_manifest_records_compaction_provenance():
    task = _task()
    task["agent_events"].extend(
        {"type": "cycle_started", "cycle": index, "ts": 10 + index}
        for index in range(40)
    )
    checkpoint = memory.build_investigation_checkpoint(task)
    manifest = checkpoint["context_manifest"]

    assert manifest["source_event_count"] > manifest["preserved_event_count"]
    assert manifest["omitted_event_count"] > 0
    assert manifest["working_memory_revision"] >= 1
    assert "working memory" in manifest["preserved_sections"]
    guidance = memory.render_checkpoint_guidance(checkpoint)
    assert "Required next action" in guidance
    assert "Already inspected" in guidance
    assert "Context manifest" in guidance
