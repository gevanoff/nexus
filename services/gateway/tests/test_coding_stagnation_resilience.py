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


def test_validation_target_never_persists_raw_argv():
    event = {
        "type": "tool_started",
        "name": "coding_run_command",
        "args": {
            "argv": [
                "/usr/bin/curl",
                "-H",
                "Authorization: Bearer super-secret-token",
                "https://example.invalid",
            ]
        },
    }

    target = resilience.inspection_target(event)

    assert target.startswith("validate curl argv:")
    assert "super-secret-token" not in target
    assert "Authorization" not in target


def test_capped_event_buffer_uses_cursor_after_rollover():
    task = _task()
    events = [{"type": "started", "run_id": "run-2", "cycle": 1, "ts": 1}]
    events.extend(
        {
            "type": "tool_started",
            "cycle": index // 10 + 1,
            "name": "coding_read_file_lines",
            "args": {"path": "services/gateway/app/old.py", "start_line": index, "line_count": 1},
            "ts": index + 2,
        }
        for index in range(998)
    )
    previous_tail = {
        "type": "cycle_started",
        "cycle": 4,
        "ts": 1001,
    }
    events.append(previous_tail)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_controller"] = {
        "schema": resilience.SCHEMA,
        "state_key": key,
        "run_id": "run-2",
        "last_cycle": 4,
        "cycles": 4,
        "progress_stagnant_cycles": 4,
        "plan_revision": 1,
        "processed_event_count": 1000,
        "processed_event_total": 1000,
        "processed_event_cursor": resilience.event_fingerprint(previous_tail),
        "interventions": [],
    }
    task["agent_events"] = events[1:] + [
        {
            "type": "tool_started",
            "cycle": 5,
            "name": "coding_search_text",
            "args": {"path": "services/gateway/app", "query": "new rollover evidence"},
            "ts": 1002,
        }
    ]
    task["agent_cycle"] = 5
    task["agent_progress_state"]["stagnant_cycles"] = 5

    checkpoint = memory.build_investigation_checkpoint(task)

    assert checkpoint["controller"]["processed_event_total"] == 1001
    assert any(
        entry.get("target") == "search services/gateway/app: new rollover evidence"
        for entry in checkpoint["inspection_ledger"]
    )


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


def test_claimed_plan_checkpoint_suppresses_assist_fallback(monkeypatch):
    task = _task(stagnant_cycles=2)
    task["agent_cycle"] = 2
    _install_workspace(monkeypatch, task)
    assert memory.process_task(task["id"]) is False

    task["agent_cycle"] = 3
    task["agent_progress_state"]["stagnant_cycles"] = 3
    task["agent_progress_state"]["observation"]["plan_revision"] = 2
    task["project_plan"]["revision"] = 2
    assert memory.process_task(task["id"]) is True

    task["agent_cycle"] = 4
    task["agent_progress_state"]["stagnant_cycles"] = 4
    task["agent_progress_state"]["observation"]["plan_revision"] = 3
    task["project_plan"]["revision"] = 3
    assert memory.process_task(task["id"]) is False

    kinds = [item["kind"] for item in task["agent_stagnation_controller"]["interventions"]]
    assert kinds == ["plan_checkpoint"]
    assert "agent_stagnation_recovery_lease" not in task


def test_stale_controller_sample_cannot_overwrite_newer_observation(monkeypatch):
    task = _task(stagnant_cycles=6)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_controller"] = {
        "schema": resilience.SCHEMA,
        "state_key": key,
        "run_id": "run-2",
        "last_cycle": 6,
        "cycles": 6,
        "progress_stagnant_cycles": 6,
        "plan_revision": 1,
        "processed_event_count": 1000,
        "processed_event_total": 1200,
        "processed_event_cursor": "newer-cursor",
        "interventions": [],
    }
    task["agent_inspection_ledger"] = [{"signature": "read:new", "target": "read new.py"}]
    task["agent_working_memory"] = {"schema": resilience.WORKING_MEMORY_SCHEMA, "state_key": key}
    task["agent_context_manifest"] = {"schema": resilience.CONTEXT_MANIFEST_SCHEMA, "state_key": key}
    _install_workspace(monkeypatch, task)
    stale = {
        "run_id": "run-2",
        "state_key": key,
        "observation_changed": True,
        "controller": {
            "schema": resilience.SCHEMA,
            "state_key": key,
            "run_id": "run-2",
            "last_cycle": 5,
            "cycles": 5,
            "progress_stagnant_cycles": 5,
            "plan_revision": 1,
            "processed_event_count": 1000,
            "processed_event_total": 1190,
            "processed_event_cursor": "stale-cursor",
            "interventions": [],
        },
        "inspection_ledger": [{"signature": "read:old", "target": "read old.py"}],
        "working_memory": {"schema": resilience.WORKING_MEMORY_SCHEMA, "state_key": key, "findings": ["stale"]},
        "context_manifest": {"schema": resilience.CONTEXT_MANIFEST_SCHEMA, "state_key": key},
    }

    assert memory._persist_observation(task["id"], stale) is False
    assert task["agent_stagnation_controller"]["last_cycle"] == 6
    assert task["agent_stagnation_controller"]["processed_event_total"] == 1200
    assert task["agent_inspection_ledger"][0]["target"] == "read new.py"


def test_stale_controller_sample_cannot_claim_checkpoint(monkeypatch):
    task = _task(stagnant_cycles=3)
    task["agent_cycle"] = 3
    stale = memory.build_investigation_checkpoint(task)
    newer = dict(stale["controller"])
    newer.update(
        {
            "last_cycle": 4,
            "cycles": 4,
            "progress_stagnant_cycles": 4,
            "processed_event_total": newer["processed_event_total"] + 1,
            "processed_event_cursor": "newer-cursor",
        }
    )
    task["agent_cycle"] = 4
    task["agent_progress_state"]["stagnant_cycles"] = 4
    task["agent_stagnation_controller"] = newer
    task["agent_inspection_ledger"] = []
    task["agent_working_memory"] = {"schema": resilience.WORKING_MEMORY_SCHEMA}
    task["agent_context_manifest"] = {"schema": resilience.CONTEXT_MANIFEST_SCHEMA}
    _install_workspace(monkeypatch, task)

    assert memory._claim_checkpoint(task["id"], stale) is False
    assert "guidance_messages" not in task
    assert "agent_investigation_checkpoint" not in task


def test_unchanged_observation_skips_task_mutation(monkeypatch):
    task = _task(stagnant_cycles=2)
    task["agent_cycle"] = 2
    mutations = {"count": 0}

    def mutate(_task_id, mutator):
        mutations["count"] += 1
        mutator(task)
        return task

    monkeypatch.setattr(memory.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(memory.cw, "mutate_task", mutate)
    monkeypatch.setattr(memory.cw, "normalize_coding_mission", lambda value: value["mission"])

    assert memory.process_task(task["id"]) is False
    assert mutations["count"] == 1
    assert memory.process_task(task["id"]) is False
    assert mutations["count"] == 1


def test_no_progress_continuation_enters_forced_action_without_fresh_recovery(monkeypatch):
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
        "interventions": [],
    }
    task["agent_previous_status"] = "paused"
    task["agent_previous_summary"] = "Coding run paused after 8 cycles without a durable state transition."
    task["agent_previous_stop_reason_code"] = "no_progress_limit"
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 1
    task["agent_events"].append({"type": "started", "run_id": "run-3", "ts": 6})
    _install_workspace(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    assert "agent_stagnation_recovery_lease" not in task
    assert task["agent_forced_action"]["status"] == "active"
    assert task["agent_forced_action"]["state_key"] == key
    assert task["agent_forced_action"]["stage"] == "continuation"


def test_granted_legacy_continuation_lease_is_retired_without_reset(monkeypatch):
    task = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_recovery_lease"] = {
        "schema": "nexus_coding_recovery_lease.v1",
        "id": f"{key}:legacy-continuation",
        "state_key": key,
        "kind": "continuation",
        "run_id": "run-2",
        "granted_cycle": 1,
        "remaining_transitions": 1,
        "status": "granted",
    }
    task["agent_cycle"] = 2
    _install_workspace(monkeypatch, task)

    assert memory._consume_recovery_lease(task["id"], task) is False
    assert task["agent_progress_state"]["stagnant_cycles"] == 8
    assert task["agent_stagnation_recovery_lease"]["status"] == "superseded"
    assert task["agent_stagnation_recovery_lease"]["remaining_transitions"] == 0


def test_guidance_interventions_are_scoped_to_run_but_recovery_credit_is_not():
    key = "durable-state"
    state_recovery = resilience.intervention_id(key, "recovery-continuation")

    assert resilience.intervention_id(key, "assist", run_id="run-2") != resilience.intervention_id(
        key,
        "assist",
        run_id="run-3",
    )
    assert state_recovery == f"{key}:recovery-continuation"
    assert state_recovery != resilience.intervention_id(
        key,
        "recovery-continuation",
        run_id="run-3",
    )


def test_legacy_consumed_continuation_is_not_revived_by_process_task(monkeypatch):
    task = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_controller"] = {
        "schema": resilience.SCHEMA,
        "state_key": key,
        "run_id": "run-2",
        "last_cycle": 8,
        "cycles": 12,
        "progress_stagnant_cycles": 8,
        "plan_revision": 1,
        "interventions": [],
    }
    task["agent_previous_status"] = "paused"
    task["agent_previous_stop_reason_code"] = "no_progress_limit"
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 1
    task["agent_events"].append({"type": "started", "run_id": "run-3", "ts": 6})
    _install_workspace(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    assert task["agent_progress_state"]["stagnant_cycles"] == 8
    assert task["agent_forced_action"]["status"] == "active"
    assert "agent_stagnation_recovery_lease" not in task


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
    assert guidance.startswith("Controller investigation checkpoint")

    second = memory.build_investigation_checkpoint(task)
    assert second["context_manifest"]["manifest_hash"] == manifest["manifest_hash"]


def test_active_plan_item_uses_consistent_clipping():
    task = _task()
    task["project_plan"]["items"][0]["summary"] = "x" * 580

    checkpoint = memory.build_investigation_checkpoint(task)

    assert checkpoint["active_plan_item"].startswith("Trace the controller:")
    assert len(checkpoint["active_plan_item"]) == 500


def test_new_durable_state_recomputes_directive_fields():
    task = _task(workspace="edited")
    task["agent_working_memory"] = {
        "schema": resilience.WORKING_MEMORY_SCHEMA,
        "state_key": "old-state",
        "revision": 7,
        "content_fingerprint": "old",
        "findings": ["Keep this established finding"],
        "unresolved_question": "Stale question",
        "next_action": "Stale action",
        "blocker": "Stale blocker",
    }

    checkpoint = memory.build_investigation_checkpoint(task)
    working = checkpoint["working_memory"]

    assert "Keep this established finding" in working["findings"]
    assert working["unresolved_question"] != "Stale question"
    assert working["next_action"] != "Stale action"
    assert working["blocker"] == ""
