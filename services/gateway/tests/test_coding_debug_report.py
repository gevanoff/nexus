from __future__ import annotations

from app import coding_debug_report as report
from app import coding_routes_guarded


TASK_ID = "code_abcdef123456"
SECRET = "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


def _task():
    return {
        "id": TASK_ID,
        "kind": "workspace",
        "status": "ready",
        "repo_url": f"https://user:{SECRET}@github.com/gevanoff/nexus.git",
        "base_branch": "main",
        "branch_name": "agent/debug-report",
        "coding_model": "coder",
        "prompt": f"Investigate the stalled agent. API_TOKEN={SECRET}",
        "created_at": 100.0,
        "updated_at": 200.0,
        "last_checkpoint_commit": "abc123",
        "agent_status": "paused",
        "agent_run_id": "run-1",
        "agent_cycle": 8,
        "agent_backend": "vllm",
        "agent_upstream_model": "example/model",
        "agent_summary": "Paused after repeated inspection.",
        "agent_error": f"Authorization: Bearer {SECRET}",
        "agent_stop_reason_code": "no_progress_limit",
        "agent_progress_state": {
            "stagnant_cycles": 8,
            "observation": {"workspace_fingerprint": "fingerprint"},
        },
        "agent_investigation_checkpoint": {
            "cycle": 4,
            "inspected_targets": ["read services/gateway/app/main.py"],
            "next_action": "Make the smallest viable edit.",
        },
        "project_plan": {
            "goal": "Add diagnostics",
            "revision": 2,
            "items": [{"id": "one", "status": "in_progress", "title": "Inspect controller"}],
        },
        "mission": {"goal": "Add diagnostics", "budget_policy": {"max_no_progress_cycles": 8}},
        "terminal_result": {
            "status": "paused",
            "stop_reason_code": "no_progress_limit",
            "summary": "No durable progress.",
            "body": f"raw backend response {SECRET}",
        },
        "guidance_messages": [
            {"ts": 150.0, "role": "user", "actor": "operator", "content": f"Check logs, token={SECRET}"}
        ],
        "agent_runs": [
            {
                "run_id": "run-1",
                "status": "paused",
                "cycle": 8,
                "summary": "No durable progress.",
                "error": f"HF_TOKEN={SECRET}",
            }
        ],
        "agent_events": [
            {"type": "assistant", "cycle": 7, "content": "The controller lost the inspection conclusion."},
            {
                "type": "tool_started",
                "cycle": 8,
                "name": "coding_read_file_lines",
                "args": {
                    "path": "services/gateway/app/coding_agent.py",
                    "start_line": 100,
                    "line_count": 40,
                    "content": f"raw file contents {SECRET}",
                },
            },
            {
                "type": "tool_finished",
                "cycle": 8,
                "name": "coding_read_file_lines",
                "result": {"ok": True, "content": f"unbounded source {SECRET}"},
            },
        ],
    }


def _install_stubs(monkeypatch, task):
    monkeypatch.setattr(report.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(report.cw, "redact_repo_url", lambda value: value.replace(f"user:{SECRET}@", ""))
    monkeypatch.setattr(report.cw, "normalize_project_plan", lambda value, fallback_goal="": value)
    monkeypatch.setattr(report.cw, "normalize_coding_mission", lambda value: value["mission"])
    monkeypatch.setattr(report.cw, "git_head", lambda _task_id: {"ok": True, "commit": "abc123"})
    monkeypatch.setattr(
        report.cw,
        "git_change_summary",
        lambda _task_id: {
            "ok": True,
            "counts": {"added": 0, "modified": 1, "removed": 0, "untracked": 0, "total": 1},
            "files": [{"path": "services/gateway/app/coding_agent.py", "status": " M", "kind": "modified"}],
            "raw": {"stdout": f"unsafe {SECRET}"},
        },
    )
    monkeypatch.setattr(
        report.cw,
        "coding_state_snapshot",
        lambda _task_id: {
            "schema": "nexus_coding_state.v1",
            "branch": {"current_head": "abc123"},
            "progress": {"cycle": 8, "current_phase": "editing"},
            "changes": {"last_edited_files": ["services/gateway/app/coding_agent.py"]},
            "validation": {"last_validation_ok": False},
            "diff_review": {"diff_reviewed_after_latest_edit": False},
            "blockers": ["No progress"],
            "recent_guidance": task["guidance_messages"],
            "recent_events": task["agent_events"],
        },
    )


def test_debug_report_is_bounded_useful_and_redacted(monkeypatch):
    task = _task()
    _install_stubs(monkeypatch, task)

    text = report.build_debug_report(TASK_ID, active_runner=False)

    assert "Nexus Coding Workspace Debug Report" in text
    assert "no_progress_limit" in text
    assert "The controller lost the inspection conclusion" in text
    assert "services/gateway/app/coding_agent.py" in text
    assert "Make the smallest viable edit" in text
    assert "[REDACTED" in text
    assert SECRET not in text
    assert "raw file contents" not in text
    assert "unbounded source" not in text
    assert "raw backend response" not in text
    assert len(text) < 150_000


def test_collection_failures_are_reported_without_aborting(monkeypatch):
    task = _task()
    _install_stubs(monkeypatch, task)
    monkeypatch.setattr(report.cw, "git_head", lambda _task_id: (_ for _ in ()).throw(RuntimeError("git unavailable")))
    monkeypatch.setattr(
        report.cw,
        "git_change_summary",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("workspace missing")),
    )
    monkeypatch.setattr(
        report.cw,
        "coding_state_snapshot",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )

    text = report.build_debug_report(TASK_ID)

    assert "git unavailable" in text
    assert "workspace missing" in text
    assert "snapshot failed" in text
    assert "Structured snapshot" in text


def test_coding_page_injection_is_idempotent_and_precedes_controller_script():
    html = '<body><script src="/static/coding.js?v=15"></script></body>'

    once = coding_routes_guarded._inject_debug_report_script(html)
    twice = coding_routes_guarded._inject_debug_report_script(once)

    assert once == twice
    assert once.count("coding_debug_report.js") == 1
    assert once.index("coding_debug_report.js") < once.index("coding.js?v=15")


def test_report_filename_is_markdown_and_task_scoped():
    filename = report.report_filename(TASK_ID)

    assert filename.startswith(f"nexus-{TASK_ID}-debug-")
    assert filename.endswith(".md")
