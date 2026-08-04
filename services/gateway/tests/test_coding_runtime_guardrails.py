from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import coding_agent
from app import coding_runtime_guardrails as guards
from app import coding_workspace as workspace


def _observation(
    cycle: int,
    *,
    fingerprint: str = "same",
    plan_revision: int = 0,
    validation_revision: int = 0,
    diff_review_revision: int = 0,
    finish_state: str = "running",
    guidance_revision: float = 0,
) -> guards.ProgressObservation:
    return guards.ProgressObservation(
        cycle=cycle,
        workspace_fingerprint=fingerprint,
        plan_revision=plan_revision,
        validation_revision=validation_revision,
        diff_review_revision=diff_review_revision,
        finish_state=finish_state,
        guidance_revision=guidance_revision,
    )


def test_eight_multi_tool_inspection_cycles_pause_on_cycle_eight() -> None:
    state = guards.ProgressState(_observation(0))
    for cycle in range(1, 8):
        # Tool count is deliberately irrelevant: the controller records one
        # observation after the complete batch.
        simulated_reads_in_batch = 4
        assert simulated_reads_in_batch > 1
        decision = guards.evaluate_cycle_progress(
            state,
            _observation(cycle),
            max_stagnant_cycles=8,
        )
        assert decision.pause is False
        assert decision.state.stagnant_cycles == cycle
        state = decision.state

    decision = guards.evaluate_cycle_progress(
        state,
        _observation(8),
        max_stagnant_cycles=8,
    )
    assert decision.pause is True
    assert decision.reason_code == "no_progress_limit"
    assert decision.state.stagnant_cycles == 8


def test_glm_route_gets_longer_hard_limit_without_changing_default_routes() -> None:
    policy = {
        "max_no_progress_cycles": 8,
        "long_model_max_no_progress_cycles": 12,
    }

    assert coding_agent._effective_max_no_progress_cycles(
        policy,
        backend="local_mlx",
        upstream_model="mlx-community/GLM-5.2-4bit",
    ) == 12
    assert coding_agent._effective_max_no_progress_cycles(
        policy,
        backend="local_vllm_fast",
        upstream_model="fast-model",
    ) == 8

    policy["max_no_progress_cycles"] = 16
    assert coding_agent._effective_max_no_progress_cycles(
        policy,
        backend="local_mlx",
        upstream_model="mlx-community/GLM-5.2-4bit",
    ) == 16


@pytest.mark.asyncio
async def test_real_agent_loop_enforces_forced_action_against_repeated_reads(monkeypatch) -> None:
    task = {
        "id": "code_test",
        "prompt": "Review this workspace for behavioral regressions and missing tests.",
        "agent_status": "queued",
        "agent_pause_requested": False,
        "agent_stop_requested": False,
        "agent_runs": [{"run_id": "run", "status": "queued"}],
        "agent_events": [],
        "guidance_messages": [],
        "project_plan": {"revision": 0},
    }
    mission = {
        "budget_policy": {
            "max_no_progress_cycles": 8,
            "max_repeated_state_reads": 100,
            "max_repeated_same_file_reads": 100,
        },
        "context_policy": {"context_reset_chars": 64_000},
        "completion_policy": {"require_file_changes": False, "require_commit_on_success": False},
    }
    executed_reads = 0
    advertised_tool_sets = []

    def mutate_task(_task_id, mutator):
        mutator(task)
        return task

    async def backend_call(req, backend, upstream_model, **kwargs):
        names = []
        for spec in req.tools or []:
            if isinstance(spec, dict):
                fn = spec.get("function")
                if isinstance(fn, dict):
                    names.append(str(fn.get("name") or ""))
            elif getattr(spec, "function", None) is not None:
                names.append(str(getattr(spec.function, "name", "") or ""))
        advertised_tool_sets.append(names)
        return {}, backend, upstream_model

    def run_tool(_task_id, name, args, *, git_token_value):
        nonlocal executed_reads
        executed_reads += 1
        return {"ok": True, "path": args.get("path")}

    batch = [
        {
            "id": f"read-{index}",
            "function": {
                "name": "coding_read_file_lines",
                "arguments": json.dumps({"path": "services/gateway/app/coding_agent.py", "start_line": index + 1}),
            },
        }
        for index in range(3)
    ]
    monkeypatch.setattr(coding_agent.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(coding_agent.cw, "mutate_task", mutate_task)
    monkeypatch.setattr(coding_agent.cw, "save_task", lambda value: value)
    monkeypatch.setattr(coding_agent.cw, "git_head", lambda _task_id: {"ok": True, "commit": "abc123"})
    monkeypatch.setattr(coding_agent.cw, "workspace_progress_fingerprint", lambda _task_id: "unchanged")
    monkeypatch.setattr(coding_agent, "_settings_for_task_owner", lambda value: {})
    monkeypatch.setattr(coding_agent, "_mission_for_task", lambda value: mission)
    monkeypatch.setattr(coding_agent, "_system_prompt", lambda *args, **kwargs: "system")
    monkeypatch.setattr(coding_agent, "_task_context", lambda value: "task")
    monkeypatch.setattr(coding_agent, "_backend_supports_tool_calling", lambda backend: True)
    monkeypatch.setattr(coding_agent, "_max_completion_tokens_for_route", lambda *args: 64)
    monkeypatch.setattr(coding_agent, "_call_backend_chat_with_retry", backend_call)
    monkeypatch.setattr(coding_agent, "_extract_assistant_message", lambda response: coding_agent.ChatMessage(role="assistant", content=None, tool_calls=batch))
    monkeypatch.setattr(coding_agent, "_extract_assistant_thinking", lambda response: "")
    monkeypatch.setattr(coding_agent, "_extract_tool_calls", lambda response: batch)
    monkeypatch.setattr(coding_agent, "_run_tool", run_tool)
    monkeypatch.setattr(coding_agent, "_checkpoint_enabled", lambda: False)
    monkeypatch.setattr(coding_agent, "_semantic_reroute_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        coding_agent,
        "decide_route",
        lambda **kwargs: SimpleNamespace(backend="test_backend", model="test_model", reason="test"),
    )

    await coding_agent._run_agent(
        "code_test",
        run_id="run",
        git_token_value=None,
        model="coder",
        auto_commit=False,
        commit_message=None,
        max_cycles=20,
        max_runtime_sec=600,
        context_reset_cycles=0,
    )

    rejected = [item for item in task["agent_events"] if item.get("type") == "forced_action_tool_rejected"]
    assert len(rejected) >= 2
    assert executed_reads < len(advertised_tool_sets) * len(batch)
    assert "coding_read_file_lines" in advertised_tool_sets[0]
    assert "coding_read_file_lines" not in advertised_tool_sets[-1]
    assert task["agent_status"] == "paused"
    assert task["agent_stop_reason_code"] == "forced_action_noncompliance"
    assert task["agent_forced_action"]["status"] == "active"


def test_noop_write_fingerprint_is_not_progress(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    path = repo / "sample.txt"
    path.write_text("same\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(workspace, "load_task", lambda _task_id: {"repo_path": str(repo)})

    baseline = workspace.workspace_progress_fingerprint("code_test")
    path.write_text("same\n", encoding="utf-8")
    unchanged = workspace.workspace_progress_fingerprint("code_test")
    path.write_text("changed\n", encoding="utf-8")
    changed = workspace.workspace_progress_fingerprint("code_test")

    assert unchanged == baseline
    assert changed != baseline


def test_failed_alternate_validation_command_does_not_reset_streak() -> None:
    previous = guards.ProgressState(
        _observation(3, validation_revision=2),
        stagnant_cycles=3,
    )
    decision = guards.evaluate_cycle_progress(
        previous,
        _observation(4, validation_revision=2),
        max_stagnant_cycles=8,
    )
    assert decision.progressed is False
    assert decision.state.stagnant_cycles == 4


def test_successful_validation_transition_after_edit_resets_streak() -> None:
    previous = guards.ProgressState(
        _observation(3, fingerprint="edited", validation_revision=2),
        stagnant_cycles=3,
    )
    decision = guards.evaluate_cycle_progress(
        previous,
        _observation(4, fingerprint="edited", validation_revision=3),
        max_stagnant_cycles=8,
    )
    assert decision.progressed is True
    assert decision.state.stagnant_cycles == 0


def test_new_guidance_resets_streak_once() -> None:
    previous = guards.ProgressState(
        _observation(5, guidance_revision=10),
        stagnant_cycles=5,
    )
    guided = guards.evaluate_cycle_progress(
        previous,
        _observation(6, guidance_revision=20),
        max_stagnant_cycles=8,
    )
    repeated = guards.evaluate_cycle_progress(
        guided.state,
        _observation(7, guidance_revision=20),
        max_stagnant_cycles=8,
    )
    assert guided.progressed is True
    assert guided.state.stagnant_cycles == 0
    assert repeated.progressed is False
    assert repeated.state.stagnant_cycles == 1


def test_cycle_progress_pause_does_not_mutate_manual_pause_flags(monkeypatch) -> None:
    task = {
        "agent_pause_requested": False,
        "agent_stop_requested": False,
        "agent_progress_state": guards.progress_state_to_dict(
            guards.ProgressState(_observation(1), stagnant_cycles=1)
        ),
        "agent_runs": [{"run_id": "run", "status": "running"}],
    }

    def transaction(_task_id, mutator):
        mutator(task)
        return task

    monkeypatch.setattr(coding_agent, "_task_transaction", transaction)
    decision = coding_agent._record_cycle_progress(
        "code_test",
        "run",
        _observation(2),
        max_stagnant_cycles=2,
    )

    assert decision.pause is True
    assert task["agent_pause_requested"] is False
    assert task["agent_stop_requested"] is False
    assert task["agent_runs"][0]["stagnant_cycles"] == 2


def test_structured_stop_reason_wins_over_legacy_summary() -> None:
    task = {
        "agent_runs": [
            {
                "run_id": "target",
                "status": "paused",
                "finished_at": 200,
                "summary": "Coding run paused after reaching its wall-clock budget.",
                "stop_reason_code": "no_progress_limit",
            }
        ]
    }
    result = guards.archive_stop_diagnostics(task, {"archived_at": 250})
    assert result["reason_code"] == "no_progress_limit"


def test_legacy_archive_reason_fallback_remains_supported() -> None:
    task = {
        "agent_runs": [
            {
                "run_id": "legacy",
                "status": "paused",
                "finished_at": 200,
                "summary": "Coding run paused after reaching its wall-clock budget.",
            }
        ]
    }
    result = guards.archive_stop_diagnostics(task, {"archived_at": 250})
    assert result["reason_code"] == "wall_clock_budget"


def test_redacted_archive_run_is_bounded_and_omits_internal_payloads() -> None:
    secret = "ghp_super_secret"
    task = {
        "agent_runs": [
            {
                "run_id": "target",
                "status": "failed",
                "finished_at": 200,
                "cycle": 4,
                "summary": f"token={secret}",
                "error": f"Authorization: Bearer {secret}",
                "prompt": "private prompt",
                "backend_body": {"raw": secret},
            }
        ]
    }
    result = guards.redacted_archive_run(
        task,
        {"archived_at": 250},
        redact=lambda value: value.replace(secret, "[REDACTED]"),
    )

    assert set(result) == {
        "run_id",
        "status",
        "stop_reason_code",
        "cycle",
        "finished_at",
        "summary",
        "error",
    }
    assert secret not in str(result)
    assert "prompt" not in result
    assert "backend_body" not in result


def test_archive_heuristics_receive_stop_finding_through_explicit_call_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task = {
        "repo_path": str(repo),
        "agent_runs": [
            {
                "run_id": "target",
                "status": "failed",
                "finished_at": 200,
                "stop_reason_code": "agent_failed",
                "error": "validation failed",
            }
        ]
    }
    findings = workspace._archive_heuristic_findings(
        task,
        {"archived_at": 250, "workspace_path": str(tmp_path)},
        {"diff": "", "files": []},
    )
    codes = {item.get("code") for item in findings}
    assert "workspace_stop_agent_failed" in codes


def test_archive_inspection_exposes_only_redacted_terminal_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive_id = "code_abcdef123456.250.deadbeef"
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    task_path = tmp_path / "task.json"
    manifest_path = tmp_path / f"{archive_id}.manifest.json"
    repo = tmp_path / "repo"
    repo.mkdir()
    task_path.write_text(
        json.dumps(
            {
                "id": "code_abcdef123456",
                "repo_path": str(repo),
                "agent_status": "failed",
                "terminal_result": {"backend_body": secret},
                "agent_runs": [
                    {
                        "run_id": "target",
                        "status": "failed",
                        "finished_at": 200,
                        "stop_reason_code": "agent_failed",
                        "summary": f"summary {secret}",
                        "error": f"error {secret}",
                        "prompt": "private run prompt",
                        "backend_body": secret,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "archive_id": archive_id,
                "task_id": "code_abcdef123456",
                "archived_at": 250,
                "workspace_path": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    archive = {
        "archive_id": archive_id,
        "task_id": "code_abcdef123456",
        "paths": {
            "task": str(task_path),
            "manifest": str(manifest_path),
        },
    }
    monkeypatch.setattr(workspace, "get_archived_task", lambda _archive_id: archive)
    monkeypatch.setattr(
        workspace,
        "_archive_diff_snapshot",
        lambda task, manifest, max_diff_chars: {"ok": True, "diff": "", "files": []},
    )
    snapshot = workspace.inspect_archived_task(archive_id)

    assert secret not in str(snapshot["terminal_run"])
    assert snapshot["terminal_run"]["stop_reason_code"] == "agent_failed"
    assert "terminal_result" not in snapshot["task"]
    assert "agent_runs" not in snapshot["task"]
    assert snapshot["stop_diagnostics"]["reason_code"] == "agent_failed"
