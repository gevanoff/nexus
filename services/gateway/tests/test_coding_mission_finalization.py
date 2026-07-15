from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_agent as ca
from app import coding_routes
from app import coding_workspace as cw


def _task(**extra):
    task = {
        "id": "task-1",
        "prompt": "Fix the bug",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "branch_name": "nexus-coder/fix",
        "agent_start_head": "start",
        "last_checkpoint_run_id": "run-1",
        "last_checkpoint_commit": "checkpoint",
    }
    task.update(extra)
    return task


def _finalizer_mocks(monkeypatch, *, changed=True, commit_ok=True):
    task = _task()
    stored = dict(task)
    monkeypatch.setattr(cw, "load_task", lambda _task_id: dict(stored))
    monkeypatch.setattr(cw, "git_status", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(cw, "git_diff", lambda *_a, **_k: {"ok": True, "changes": {"counts": {"total": 1}}})
    monkeypatch.setattr(cw, "git_change_summary", lambda *_a, **_k: {"ok": True, "counts": {"total": 1 if changed else 0}})
    monkeypatch.setattr(cw, "git_head", lambda *_a, **_k: {"ok": True, "commit": "checkpoint" if not changed else "start"})
    monkeypatch.setattr(cw, "commit_task", lambda *_a, **_k: {"ok": commit_ok, "last_commit": "final", "error": "commit failed" if not commit_ok else ""})
    monkeypatch.setattr(cw, "coding_state_snapshot", lambda *_a, **_k: {"validation": {"validation_after_latest_edit": True}, "diff_review": {"diff_reviewed_after_latest_edit": True}})
    monkeypatch.setattr(cw, "mutate_task", lambda _task_id, fn: fn(stored) or stored)
    return stored


def test_coding_mission_contract_defaults():
    mission = cw.normalize_coding_mission(_task())
    assert mission["schema"] == "nexus_coding_mission.v1"
    assert mission["completion_policy"]["commit_policy"] == "always_on_success"
    assert mission["completion_policy"]["require_commit_on_success"] is True
    assert mission["publish_policy"]["push"] == "never"
    assert mission["context_policy"]["context_reset_cycles"] == 0


def test_coding_finalization_commits_on_success(monkeypatch):
    _finalizer_mocks(monkeypatch, changed=True)
    result = ca.finalize_successful_run("task-1", finish_summary="Fix it", run_id="run-1")
    assert result["ok"] is True
    assert result["final_commit"] == "final"


def test_coding_finalization_uses_existing_checkpoint_commit(monkeypatch):
    _finalizer_mocks(monkeypatch, changed=False)
    result = ca.finalize_successful_run("task-1", run_id="run-1")
    assert result["ok"] is True
    assert result["final_commit"] == "checkpoint"


def test_coding_finalization_push_on_success(monkeypatch):
    _finalizer_mocks(monkeypatch)
    pushed = []
    monkeypatch.setattr(cw, "push_task", lambda *_a, **_k: pushed.append(True) or {"ok": True})
    mission = {"publish_policy": {"push": "on_success"}}
    result = ca.finalize_successful_run("task-1", mission=mission, run_id="run-1")
    assert result["ok"] is True and pushed and result["pushed_at"]


def test_coding_finalization_draft_pr_on_success(monkeypatch):
    _finalizer_mocks(monkeypatch)
    monkeypatch.setattr(cw, "push_task", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(cw, "create_pull_request", lambda *_a, **_k: {"ok": True, "url": "https://example/pr/1", "number": 1})
    mission = {"publish_policy": {"draft_pr": "on_success", "pr_title": "Fix"}}
    result = ca.finalize_successful_run("task-1", mission=mission, run_id="run-1")
    assert result["pr_url"] == "https://example/pr/1"
    assert result["pr_number"] == 1


def test_coding_finalization_fails_if_commit_fails(monkeypatch):
    _finalizer_mocks(monkeypatch, commit_ok=False)
    result = ca.finalize_successful_run("task-1", run_id="run-1")
    assert result["ok"] is False
    assert result["finalization_status"] == "failed_finalization"


def test_coding_context_reset_uses_state_snapshot():
    source = Path(ca.__file__).read_text(encoding="utf-8")
    assert "Controller state snapshot (authoritative)" in source
    assert "Use the controller-provided state snapshot as authoritative" in source
    assert "Inspect current state before making assumptions" not in source


def test_coding_context_reset_cycles_zero_disables_interval_reset():
    assert ca._context_reset_cycles(0) == 0
    source = Path(ca.__file__).read_text(encoding="utf-8")
    assert "context_reset_cycles > 0" in source


def test_coding_progress_budget_detects_repeated_diff_loop():
    assert ca._state_read_signature("coding_git_diff", {}) == "coding_git_diff"
    assert ca._repeated_state_read_decision(6, 6) == "guide"
    assert ca._repeated_state_read_decision(7, 6) == "pause"


def test_coding_ui_horizon_limits_match_api():
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "coding.html").read_text(encoding="utf-8")
    js = (static_root / "coding.js").read_text(encoding="utf-8")
    assert 'id="agentMaxCycles" type="number" min="4" max="1000"' in html
    assert "Math.min(1000" in js
    field = coding_routes.CodingCreateAndRunRequest.model_fields["max_cycles"]
    assert any(getattr(item, "le", None) == 1000 for item in field.metadata)


def test_coding_ui_terminal_result_is_shared_by_meta_and_log_rendering():
    js = (Path(__file__).resolve().parents[1] / "app" / "static" / "coding.js").read_text(encoding="utf-8")
    render_agent = js[js.index("function renderAgent(task)") : js.index("function renderChangeSummary")]
    declaration = 'const terminal = task && task.terminal_result'
    assert render_agent.count(declaration) == 1
    assert render_agent.index(declaration) < render_agent.index("if (els.agentMeta)")
    assert render_agent.index(declaration) < render_agent.index("if (els.agentLog)")


def test_scripted_coding_mission_finishes_with_real_branch_commit(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspaces"
    tasks_root = tmp_path / "tasks"
    task_id = "code_abcdef123456"
    repo = workspace_root / task_id / "repo"
    repo.mkdir(parents=True)
    tasks_root.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Nexus Test",
            "GIT_AUTHOR_EMAIL": "nexus@example.com",
            "GIT_COMMITTER_NAME": "Nexus Test",
            "GIT_COMMITTER_EMAIL": "nexus@example.com",
        }
    )
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True, env=env, capture_output=True, text=True)
    run("init")
    run("branch", "-M", "main")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    run("add", "app.py")
    run("commit", "-m", "base")
    start_head = run("rev-parse", "HEAD").stdout.strip()
    run("switch", "-c", "nexus-coder/scripted")
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    task = _task(
        id=task_id,
        schema=cw.SCHEMA,
        status="ready",
        workspace_path=str(workspace_root / task_id),
        repo_path=str(repo),
        branch_name="nexus-coder/scripted",
        agent_start_head=start_head,
        commands=[{"ts": 3, "label": "command", "ok": True, "argv": ["python", "-m", "py_compile", "app.py"]}],
        agent_events=[
            {"ts": 2, "type": "tool_finished", "name": "coding_replace_text", "result": {"ok": True}},
            {"ts": 4, "type": "tool_finished", "name": "coding_git_diff", "result": {"ok": True}},
        ],
    )
    task["mission"] = cw.normalize_coding_mission(task)
    (tasks_root / f"{task_id}.json").write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(cw, "workspace_root", lambda: workspace_root)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tasks_root)
    for key, value in env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)

    result = ca.finalize_successful_run(task_id, finish_summary="Update value", run_id="run-1")

    assert result["ok"] is True
    assert result["final_commit"] != start_head
    assert run("status", "--porcelain").stdout == ""
    assert run("log", "-1", "--pretty=%s").stdout.strip() == "Update value"
