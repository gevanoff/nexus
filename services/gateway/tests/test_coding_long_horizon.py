from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from app import coding_backend_failover as failover
from app import coding_execution_dispatch as dispatch
from app import coding_mission_acceptance_epoch as epoch
from app import coding_resume_convergence_hardening as convergence
from fastapi import HTTPException

OLD = "mlx-community/GLM-5.2-4bit"
NEW = "mlx-community/GLM-5.3-mixed-4_5bit"
PATHS = ["gateway.json", "mlx.json", "defaults.py"]


class Store:
    def __init__(self, task=None):
        self.task = copy.deepcopy(task or {"id": "code_long", "agent_run_id": "run-a"})

    def load_task(self, _task_id):
        return copy.deepcopy(self.task)

    def mutate_task(self, _task_id, apply):
        apply(self.task)
        return self.load_task(_task_id)


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Import the complete installed controller, including all acceptance and
    # evidence wrappers, so this exercises their production composition.
    from app import coding_routes_guarded as routes
    from app import coding_workspace as cw

    agent = routes.guarded_agent._agent
    task_id = "code_abc123def456"
    root = tmp_path / "workspaces"
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    repo = root / task_id / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(cw, "workspace_root", lambda: root)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tasks)
    git(repo, "init", "-b", "main")
    (repo / "gateway.json").write_text(
        json.dumps({"coder": OLD, "glm-5.2": OLD}) + "\n"
    )
    (repo / "mlx.json").write_text(json.dumps({"default_model": OLD}) + "\n")
    (repo / "defaults.py").write_text(f'DEFAULT_MODEL = "{OLD}"\n')
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "mission base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "fixture/coder-migration")
    task = {
        "schema": cw.SCHEMA,
        "id": task_id,
        "status": "ready",
        "prompt": "Update coder/default MLX to GLM-5.3, preserving the explicit glm-5.2 legacy alias.",
        "repo_url": "https://github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "branch_name": "fixture/coder-migration",
        "workspace_path": str(repo.parent),
        "repo_path": str(repo),
        "agent_run_id": "run-a",
        "agent_start_head": base,
        "agent_events": [],
        "commands": [],
        "agent_cycle": 1,
        "agent_backend": "local_mlx",
        "agent_upstream_model": OLD,
    }
    cw.save_task(task)
    agent._append_event(task_id, {"type": "started", "run_id": "run-a"})
    epoch.ensure_epoch(cw, task_id)

    def call(name, **args):
        current = cw.load_task(task_id)
        allowed, rejection = agent.forced_action.evaluate_tool_call(
            current,
            name=name,
            args=args,
            is_validation_command=agent._is_validation_command,
        )
        assert allowed, rejection
        result = agent._run_tool(task_id, name, args, git_token_value=None)
        agent._append_event(
            task_id,
            {
                "type": "tool_finished",
                "name": name,
                "args": args,
                "result": result,
                "ts": time.time(),
            },
        )
        return result

    return SimpleNamespace(
        cw=cw,
        agent=agent,
        task_id=task_id,
        repo=repo,
        base=base,
        call=call,
        routes=routes,
    )


def ground(w, *, forced=True):
    from app import coding_stagnation_resilience as resilience

    # Initial orientation is legal before the bounded remediation gate. The
    # later gate can reuse exact repository evidence without an extra search.
    for path in PATHS:
        result = w.call(
            "coding_read_file_lines", path=path, start_line=1, line_count=10
        )
        assert result.get("ok", True), result
    task = w.cw.load_task(w.task_id)
    forced_state = w.agent.forced_action.activate(
        task,
        state_key=resilience.durable_state_key(task),
        run_id="run-a",
        cycle=1,
        stage="interrupt",
        required_action="Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        action_kind="edit",
    )
    if forced:
        w.cw.mutate_task(
            w.task_id, lambda task: task.update(agent_forced_action=forced_state)
        )
    note = (
        "Root cause: coder and MLX defaults still select the previous model.\n"
        "Repository evidence: gateway.json:1-1, mlx.json:1-1 and defaults.py:1-1 contain the default model routes.\n"
        "Competing explanation checked: the explicit glm-5.2 alias intentionally remains legacy.\n"
        "Expected result: update the three defaults together and preserve the legacy alias."
    )
    result = w.call("coding_update_plan", note=note)
    assert result.get("ok"), result
    return w.agent.forced_action.active_state(w.cw.load_task(w.task_id))


def test_ordinary_grounded_execution_can_open_batch_without_stagnation(workspace):
    w = workspace
    assert ground(w, forced=False) == {}
    replace(w, "gateway.json")
    batch = convergence.active_edit_batch(w.cw.load_task(w.task_id))
    assert batch["attempts"] == 1 and set(batch["paths"]) == set(PATHS)
    for path in PATHS[1:]:
        replace(w, path)
    task = w.cw.load_task(w.task_id)
    assert task[convergence.EDIT_BATCH_KEY]["attempts"] == 3
    assert "GLM-5.2" in task["agent_hypothesis_lifecycle"]["verified_evidence_digest"]


def replace(w, path):
    old = f'"coder": "{OLD}"' if path == "gateway.json" else OLD
    new = f'"coder": "{NEW}"' if path == "gateway.json" else NEW
    result = w.call("coding_replace_text", path=path, old_text=old, new_text=new)
    assert result.get("ok"), result
    return result


def test_real_controller_allows_coherent_migration_then_forces_validation_and_review(
    workspace,
):
    w = workspace
    state = ground(w)
    assert set(state["durable_hypothesis_note_causal_targets"]) == set(PATHS)
    for index, path in enumerate(PATHS):
        replace(w, path)
        task = w.cw.load_task(w.task_id)
        batch = convergence.active_edit_batch(task)
        assert batch["attempts"] == index + 1
        assert (
            w.agent.forced_action.active_state(task)["stage"] == "coherent_edit_batch"
        )
        snapshot = w.cw.coding_state_snapshot(w.task_id)
        assert not snapshot["validation"]["validation_after_latest_edit"]
        assert not snapshot["diff_review"]["diff_reviewed_after_latest_edit"]
        allowed, rejection = w.agent.forced_action.evaluate_tool_call(
            task,
            name="coding_finish",
            args={"success": True},
            is_validation_command=w.agent._is_validation_command,
        )
        assert not allowed and rejection["error"] == "forced_action_tool_rejected"
    # No semantic attempt is allowed while the coherent repair is still open.
    blocked = w.agent._run_tool(
        w.task_id, "coding_finish", {"success": True}, git_token_value=None
    )
    assert blocked["error"] == "forced_action_tool_rejected"
    result = w.call(
        "coding_run_command", argv=["python", "-m", "compileall", "-q", "defaults.py"]
    )
    assert result["ok"], result
    state = w.agent.forced_action.active_state(w.cw.load_task(w.task_id))
    assert state["action_kind"] == "review", state
    allowed, _ = w.agent.forced_action.evaluate_tool_call(
        w.cw.load_task(w.task_id),
        name="coding_finish",
        args={"success": True},
        is_validation_command=w.agent._is_validation_command,
    )
    assert not allowed
    blocked = w.agent._run_tool(
        w.task_id, "coding_finish", {"success": True}, git_token_value=None
    )
    assert blocked["error"] == "forced_action_tool_rejected"
    assert not any(
        e["type"] == "semantic_acceptance_review"
        for e in w.cw.load_task(w.task_id)["agent_events"]
    )
    assert w.call("coding_git_diff")["ok"]
    assert (
        w.agent.forced_action.active_state(w.cw.load_task(w.task_id))["action_kind"]
        == "finish"
    )
    assert json.loads((w.repo / "gateway.json").read_text()) == {
        "coder": NEW,
        "glm-5.2": OLD,
    }


def test_checkpoint_snapshot_and_rematerialized_prompt_keep_immutable_mission_delta(
    workspace, monkeypatch
):
    w = workspace
    ground(w)
    replace(w, "gateway.json")
    git(w.repo, "add", "gateway.json")
    git(w.repo, "commit", "-m", "checkpoint")
    checkpoint = git(w.repo, "rev-parse", "HEAD")
    w.cw.mutate_task(
        w.task_id,
        lambda task: task.update(
            agent_run_id="run-b",
            agent_start_head=checkpoint,
            last_checkpoint_commit=checkpoint,
        ),
    )
    snapshot = w.cw.coding_state_snapshot(w.task_id)
    assert snapshot["working_tree"]["clean"] is True
    assert snapshot["working_tree"]["changed_files"] == []
    assert snapshot["mission_delta"]["has_delta"] is True
    assert snapshot["mission_delta"]["base_head"] == w.base
    assert snapshot["mission_delta"]["changed_files"] == ["gateway.json"]
    assert snapshot["mission_delta"]["checkpoint_committed"] is True
    assert snapshot["run_delta"]["mutation_count"] == 0
    assert convergence.active_edit_batch(w.cw.load_task(w.task_id))["attempts"] == 1
    from app.models import ChatCompletionRequest, ChatMessage

    monkeypatch.setattr(w.agent, "_backend_supports_tool_calling", lambda _: True)
    req = ChatCompletionRequest(
        model="coder",
        messages=[
            ChatMessage(role="system", content="You are Nexus Coding Agent"),
            ChatMessage(
                role="user",
                content='Original request\n\nController state snapshot (authoritative):\n{"schema":"nexus_coding_state.v1","stale":true}',
            ),
        ],
    )
    fresh, _, _ = dispatch.materialize_request(
        w.agent,
        req,
        w.cw.load_task(w.task_id),
        source_backend="native",
        backend="native",
        upstream_model=NEW,
    )
    text = "\n".join(message.content or "" for message in fresh.messages)
    assert '"stale": true' not in text and '"stale":true' not in text
    assert '"has_delta": true' in text and '"clean": true' in text
    assert "A clean working tree does not imply" in text
    assert "historical audit context, not current causal truth" not in text


def test_incident_reviewer_outage_checkpoint_resume_rejection_repair_and_acceptance(
    workspace, monkeypatch
):
    from app import coding_terminal_acceptance_hardening as terminal

    w = workspace
    ground(w)
    replace(w, "gateway.json")  # An intentionally incomplete mission checkpoint.
    reviews = []
    decisions = iter(["unavailable", "reject", "accept"])

    async def review(task_id, task, *, diff_text):
        decision = next(decisions)
        reviews.append((decision, diff_text))
        assert w.base in diff_text
        if decision == "accept":
            assert NEW in (w.repo / "defaults.py").read_text()
            assert json.loads((w.repo / "mlx.json").read_text())["default_model"] == NEW
            assert json.loads((w.repo / "gateway.json").read_text())["glm-5.2"] == OLD
        return {
            "accepted": decision == "accept",
            "review_error": decision == "unavailable",
            "reason": "all eligible reviewer routes returned unusable responses"
            if decision == "unavailable"
            else "mlx.json:1 and defaults.py:1 still select GLM-5.2"
            if decision == "reject"
            else "targeted defaults migrated; explicit legacy alias preserved",
            "causal_alignment": decision == "accept",
            "existing_mechanism_checked": True,
            "acceptance_criteria_checked": True,
            "fingerprint": terminal.semantic_acceptance_fingerprint(
                task, diff_text=diff_text
            ),
        }

    monkeypatch.setattr(w.routes.guarded_agent, "_semantic_acceptance_review", review)
    assert w.call(
        "coding_run_command", argv=["python", "-m", "compileall", "-q", "defaults.py"]
    )["ok"]
    assert w.call("coding_git_diff")["ok"]
    unavailable = w.call("coding_finish", success=True, summary="Review migration")
    assert unavailable["error"] == "semantic_reviewer_unavailable", unavailable
    assert unavailable["interrupted"] and unavailable["resumable"]
    assert not w.cw.load_task(w.task_id).get("coding_semantic_rejection_guard")
    failover.record_full_timeout(w.cw, w.task_id, "local_mlx", OLD)
    git(w.repo, "add", ".")
    git(w.repo, "commit", "-m", "interrupted checkpoint")
    checkpoint = git(w.repo, "rev-parse", "HEAD")
    w.cw.mutate_task(
        w.task_id,
        lambda task: task.update(
            agent_run_id="run-b",
            agent_start_head=checkpoint,
            last_checkpoint_commit=checkpoint,
        ),
    )
    snapshot = w.cw.coding_state_snapshot(w.task_id)
    assert snapshot["working_tree"]["changed_files"] == []
    assert snapshot["mission_delta"]["has_delta"]
    assert snapshot["mission_delta"]["base_head"] == w.base
    assert snapshot["coding_backend_cooldowns"][0]["active"]
    rejected = w.call("coding_finish", success=True, summary="Retry independent review")
    assert rejected["error"] == "semantic_acceptance_rejected", rejected
    repeat = w.agent._run_tool(
        w.task_id, "coding_finish", {"success": True}, git_token_value=None
    )
    assert repeat["error"] in {
        "semantic_acceptance_state_unchanged",
        "semantic_acceptance_repeat_blocked",
    }
    assert len(reviews) == 2
    # A concrete blocker still exits even when the exact-state rejection guard
    # disallows another successful finish attempt.
    blocker = w.agent._run_tool(
        w.task_id,
        "coding_finish",
        {
            "success": False,
            "summary": "MLX default migration needs further grounded repair.",
        },
        git_token_value=None,
    )
    assert blocker["ok"] and blocker["success"] is False, blocker
    state = w.agent.forced_action.active_state(w.cw.load_task(w.task_id))
    assert state["action_kind"] != "finish", state
    assert w.call(
        "coding_refute_hypothesis",
        reason="Only the gateway route was changed; MLX defaults remain old.",
        contradicting_evidence="mlx.json:1 and defaults.py:1 still select GLM-5.2",
    )["ok"]
    for path in PATHS[1:]:
        w.call("coding_read_file_lines", path=path, start_line=1, line_count=1)
    result = w.call(
        "coding_update_plan",
        note=(
            "Root cause: the checkpoint migrated only the gateway coder route, leaving MLX defaults behind.\n"
            "Repository evidence: mlx.json:1-1 and defaults.py:1-1 still select the old default.\n"
            "Competing explanation checked: the versioned gateway alias should stay on GLM-5.2.\n"
            "Expected result: migrate the remaining two defaults while preserving the checkpoint route and legacy alias."
        ),
    )
    assert result["ok"], result
    for path in PATHS[1:]:
        replace(w, path)
    assert w.call(
        "coding_run_command", argv=["python", "-m", "compileall", "-q", "defaults.py"]
    )["ok"]
    assert w.call("coding_git_diff")["ok"]
    accepted = w.call(
        "coding_finish",
        success=True,
        summary="Default migration completed and legacy alias preserved",
    )
    assert accepted["ok"] and accepted["success"], accepted
    assert w.cw.coding_state_snapshot(w.task_id)["mission_acceptance"][
        "semantic_accepted"
    ]
    final = w.agent.finalize_successful_run(
        w.task_id, finish_summary="Default migration completed", run_id="run-b"
    )
    assert final["ok"], final
    assert len(reviews) == 3


def test_batch_budget_includes_failed_edits_and_cannot_be_renewed_by_resume(workspace):
    w = workspace
    ground(w)
    replace(w, "gateway.json")
    for expected_attempt in (2, 3, 4):
        result = w.call(
            "coding_replace_text",
            path="mlx.json",
            old_text="missing text",
            new_text="anything",
        )
        assert not result["ok"]
        task = w.cw.load_task(w.task_id)
        assert task[convergence.EDIT_BATCH_KEY]["attempts"] == expected_attempt
        w.cw.mutate_task(
            w.task_id,
            lambda task, attempt=expected_attempt: task.update(
                agent_run_id=f"run-{attempt}"
            ),
        )
    assert not convergence.active_edit_batch(w.cw.load_task(w.task_id))
    state = w.agent.forced_action.active_state(w.cw.load_task(w.task_id))
    assert state["action_kind"] == "validate", state
    spec = next(
        spec
        for spec in w.agent._tool_specs_for_task(w.cw.load_task(w.task_id))
        if spec.function.name == "coding_finish"
    )
    assert spec.function.parameters["properties"]["success"]["const"] is False
    blocked = w.agent._run_tool(
        w.task_id, "coding_finish", {"success": True}, git_token_value=None
    )
    assert blocked["error"] == "forced_action_tool_rejected"


def test_batch_scope_and_hypothesis_change_do_not_grant_free_editing(workspace):
    w = workspace
    ground(w)
    replace(w, "gateway.json")
    blocked = w.agent._run_tool(
        w.task_id,
        "coding_write_file",
        {"path": "unrelated.py", "content": "pass"},
        git_token_value=None,
    )
    assert blocked["error"] == "forced_action_tool_rejected"
    assert not (w.repo / "unrelated.py").exists()
    w.cw.update_project_plan(w.task_id, note="New ungrounded theory", actor="operator")
    assert not convergence.active_edit_batch(w.cw.load_task(w.task_id))


def test_concurrent_edits_cannot_both_spend_the_last_batch_attempt(workspace):
    from concurrent.futures import ThreadPoolExecutor

    w = workspace
    ground(w)
    for path in PATHS:
        replace(w, path)

    def edit(path):
        return w.agent._run_tool(
            w.task_id,
            "coding_replace_text",
            {"path": path, "old_text": NEW, "new_text": NEW + "-repair"},
            git_token_value=None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, PATHS[1:]))
    assert sum(bool(result.get("ok")) for result in results) == 1, results
    assert any(
        result.get("error") == "forced_action_tool_rejected" for result in results
    )
    assert w.cw.load_task(w.task_id)[convergence.EDIT_BATCH_KEY]["attempts"] == 4


def test_harness_batch_closes_at_diff_without_granting_command_authority(workspace):
    w = workspace
    w.cw.mutate_task(w.task_id, lambda task: task.update(kind="harness_eval"))
    ground(w)
    for path in PATHS:
        replace(w, path)
    state = w.agent.forced_action.active_state(w.cw.load_task(w.task_id))
    assert state["stage"] == "coherent_edit_batch"
    assert "coding_run_command" not in state["allowed_tools"]
    assert w.call("coding_git_diff")["ok"]
    assert not convergence.active_edit_batch(w.cw.load_task(w.task_id))
    assert (
        w.agent.forced_action.active_state(w.cw.load_task(w.task_id))["action_kind"]
        == "finish"
    )


def test_refutation_can_close_an_open_batch_without_reauthorizing_old_note(workspace):
    w = workspace
    ground(w)
    replace(w, "gateway.json")
    result = w.call(
        "coding_refute_hypothesis",
        reason="New evidence contradicts the chosen MLX default path.",
        contradicting_evidence="mlx.json default differs from the grounded assumption",
    )
    assert result["ok"] and result["refuted"], result
    state = w.agent.forced_action.active_state(w.cw.load_task(w.task_id))
    assert state["action_kind"] == "evidence"
    assert "coding_replace_text" not in state["allowed_tools"]
    assert not convergence.active_edit_batch(w.cw.load_task(w.task_id))


def test_cooldown_identity_expiry_recovery_and_resume(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(failover.time, "time", lambda: now[0])
    store = Store()
    lanes = [
        {"backend": "local_mlx", "upstream_model": OLD},
        {"backend": "local_mlx", "upstream_model": NEW},
        {"backend": "local_vllm_fast", "upstream_model": "fallback"},
    ]
    failover.record_full_timeout(store, "code_long", "local_mlx", OLD)
    store.task["agent_run_id"] = "run-b"
    restored = Store(json.loads(json.dumps(store.task)))
    assert failover.filter_task_candidates(lanes, restored.task) == lanes[1:]
    assert failover.filter_task_candidates(lanes, Store().task) == lanes
    record = failover.cooldown_state(restored.task)[0]
    assert record["last_failure_run_id"] == "run-a" and record["active"]
    now[0] = record["retry_after"]
    assert failover.filter_task_candidates(lanes, restored.task) == lanes
    failover.record_success(restored, "code_long", "local_mlx", OLD)
    assert failover.cooldown_state(restored.task)[0]["failures"] == 0
    failover.record_full_timeout(restored, "code_long", "local_mlx", OLD)
    assert (
        failover.cooldown_state(restored.task)[0]["retry_after"]
        == now[0] + failover.COOLDOWN_SEC
    )


def test_older_inflight_success_does_not_erase_newer_timeout(monkeypatch):
    store = Store()
    monkeypatch.setattr(failover.time, "time", lambda: 100.0)
    failover.record_full_timeout(store, "code_long", "local_mlx", OLD)
    failover.record_success(store, "code_long", "local_mlx", OLD, started_at=99.0)
    assert failover.cooldown_state(store.task)[0]["active"]


@pytest.mark.parametrize("retry_count", [0, 1])
def test_actual_failover_dispatch_and_admission_share_task_cooldown(
    workspace, monkeypatch, retry_count
):
    from app.models import ChatCompletionRequest, ChatMessage

    w = workspace
    agent = w.agent
    guarded = w.routes.guarded_agent
    selected, events = [], []

    class Admission:
        async def acquire(self, backend, capability):
            assert capability == "chat"

        def release(self, backend, capability):
            assert capability == "chat"

    lanes = [
        {"backend": "local_mlx", "upstream_model": OLD, "ready": True, "available": 1},
        {
            "backend": "local_vllm_fast",
            "upstream_model": "fallback",
            "ready": True,
            "available": 1,
        },
    ]

    async def backend_call(req, backend, model):
        selected.append((backend, model))
        if backend == "local_mlx":
            raise HTTPException(
                502, detail={"error": "ReadTimeout: read timeout after 600s"}
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(agent, "get_admission_controller", lambda: Admission())
    monkeypatch.setattr(
        agent, "_rank_coding_backend_candidates", lambda *a, **kw: lanes
    )
    monkeypatch.setattr(agent, "_max_completion_tokens_for_route", lambda *a: 256)
    monkeypatch.setattr(agent, "_backend_retry_count", lambda: retry_count)
    monkeypatch.setattr(
        agent, "_append_event", lambda task_id, event: events.append(event)
    )
    monkeypatch.setattr(agent, "call_backend_chat", backend_call)
    call = dispatch.build_failover_call(w.cw, guarded)
    req = ChatCompletionRequest(
        model="coder",
        messages=[ChatMessage(role="system", content="Independent review")],
    )
    first = call(req, "local_mlx", OLD, task_id=w.task_id, cycle=1)
    if retry_count:
        assert asyncio.run(first)[1] == "local_vllm_fast"
    else:
        with pytest.raises(HTTPException, match="ReadTimeout"):
            asyncio.run(first)
    # Even a timeout on the final retry is durable before the error escapes.
    assert failover.cooldown_state(w.cw.load_task(w.task_id))[0]["active"]
    w.cw.mutate_task(w.task_id, lambda task: task.update(agent_run_id="run-b"))
    assert (
        asyncio.run(call(req, "local_mlx", OLD, task_id=w.task_id, cycle=1))[1]
        == "local_vllm_fast"
    )
    assert selected.count(("local_mlx", OLD)) == 1
    selected_events = [event for event in events if event["type"] == "backend_selected"]
    assert selected_events[-1]["coding_backend_cooldowns"][0]["active"]
    retry_after = failover.cooldown_state(w.cw.load_task(w.task_id))[0]["retry_after"]
    monkeypatch.setattr(failover.time, "time", lambda: retry_after + 1)

    async def recovered_call(req, backend, model):
        return {"choices": [{"message": {"content": "recovered"}}]}

    monkeypatch.setattr(agent, "call_backend_chat", recovered_call)
    assert (
        asyncio.run(call(req, "local_mlx", OLD, task_id=w.task_id, cycle=2))[1]
        == "local_mlx"
    )
    assert failover.cooldown_state(w.cw.load_task(w.task_id))[0]["failures"] == 0


def test_selected_user_model_cannot_bypass_task_cooldown(workspace, monkeypatch):
    from app.models import ChatCompletionRequest, ChatMessage

    w = workspace
    calls = []
    monkeypatch.setattr(w.agent.user_llm, "is_user_model_id", lambda _: True)
    monkeypatch.setattr(
        w.agent.user_llm, "parse_user_model_id", lambda _: ("fixture", "model")
    )
    monkeypatch.setattr(w.agent.user_llm, "user_backend_name", lambda _: "user-fixture")

    async def user_call(*args, **kwargs):
        calls.append(1)
        raise HTTPException(
            502, detail={"error": "ReadTimeout: read timeout after 600s"}
        )

    monkeypatch.setattr(w.agent.user_llm, "call_user_chat", user_call)
    req = ChatCompletionRequest(
        model="user-fixture:model", messages=[ChatMessage(role="user", content="work")]
    )
    call = dispatch.build_failover_call(w.cw, w.routes.guarded_agent)
    with pytest.raises(HTTPException, match="ReadTimeout"):
        asyncio.run(call(req, "user-fixture", "model", task_id=w.task_id, cycle=1))
    w.cw.mutate_task(w.task_id, lambda task: task.update(agent_run_id="run-b"))
    with pytest.raises(HTTPException, match="task cooldown"):
        asyncio.run(call(req, "user-fixture", "model", task_id=w.task_id, cycle=1))
    assert len(calls) == 1


@pytest.mark.parametrize("action", ["validate", "review", "diff_review"])
def test_success_finish_policy_cannot_spend_a_review_but_blocker_can_exit(
    monkeypatch, action
):
    from app import coding_forced_action as forced

    monkeypatch.setattr(
        forced,
        "active_state",
        lambda _: {
            "action_kind": action,
            "allowed_tools": ["coding_finish"],
            "required_action": action,
        },
    )
    allowed, result = forced.evaluate_tool_call(
        {},
        name="coding_finish",
        args={"success": True},
        is_validation_command=lambda _: True,
    )
    assert not allowed and result["error"] == "forced_action_tool_rejected"
    allowed, _ = forced.evaluate_tool_call(
        {},
        name="coding_finish",
        args={
            "success": False,
            "summary": "Required test runner is missing from the workspace.",
        },
        is_validation_command=lambda _: True,
    )
    assert allowed


def test_command_guidance_is_in_model_tool_schema(workspace):
    spec = next(
        spec
        for spec in workspace.agent._tool_specs()
        if spec.function.name == "coding_run_command"
    )
    assert "argv file paths resolve relative to cwd" in spec.function.description
    assert "app/backends.py" in spec.function.description


def test_debug_report_labels_runtime_authorities(workspace):
    from app import coding_debug_report

    w = workspace
    w.cw.mutate_task(w.task_id, lambda task: task.update(agent_max_runtime_sec=1234))
    report = coding_debug_report.collect_debug_snapshot(w.task_id)
    provenance = report["runtime_policy_provenance"]
    assert provenance["runner_effective_max_runtime_sec"] == 1234
    assert "max_runtime_sec" in provenance["mission_budget_policy"]
    assert "CODING_AGENT_MAX_RUNTIME_SEC" in provenance["effective_runtime_config"]
    assert "mission_delta" in report["durable_state"]
    json.dumps(report, allow_nan=False)
