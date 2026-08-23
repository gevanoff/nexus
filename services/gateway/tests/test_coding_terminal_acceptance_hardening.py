from __future__ import annotations

from pathlib import Path

from app import coding_routes_guarded
from app import coding_terminal_acceptance_hardening as hardening


class _Agent:
    def __init__(self, cw):
        self.cw = cw
        self.events = []
        self._run_tool = None

    def _append_event(self, task_id, event):
        recorded = dict(event)
        self.events.append(recorded)
        self.cw.tasks[task_id].setdefault("agent_events", []).append(recorded)


class _CW:
    def __init__(self, task):
        self.tasks = {task["id"]: task}
        self.coding_state_snapshot = lambda _task_id: {
            "changes": {"last_edit_at": 10.0},
            "validation": {
                "last_validation_command": ["git", "log", "--oneline"],
                "last_validation_ok": True,
                "last_validation_at": 20.0,
                "validation_after_latest_edit": True,
            },
        }

    def load_task(self, task_id):
        return self.tasks[task_id]


class _Guarded:
    def __init__(self):
        self.calls = 0
        self._run_tool_with_semantic_acceptance = self._run

    @staticmethod
    def _run_delta_diff(_task_id, _task):
        return "diff --git a/app.py b/app.py\n+fixed\n"

    def _run(self, _task_id, name, _args, *, git_token_value):
        assert git_token_value is None
        self.calls += 1
        if name == "coding_finish":
            return {
                "ok": False,
                "success": False,
                "error": "semantic_acceptance_rejected",
                "summary": "review rejected",
                "semantic_review": {
                    "accepted": False,
                    "reason": "causal evidence is incomplete",
                    "review_error": False,
                },
            }
        return {"ok": True}


class _WorkPhases:
    @staticmethod
    def is_validation_command(argv):
        return argv in (
            ["python3", "-m", "py_compile", "app.py"],
            ["pytest", "-q"],
        )


def _task():
    return {
        "id": "code-test",
        "agent_cycle": 12,
        "project_plan": {
            "revision": 2,
            "note": "Root cause: early return skips management metadata.",
            "items": [],
        },
        "agent_forced_action": {
            "causal_evidence_targets": ["app.py"],
            "causal_evidence_ranges": [{"path": "app.py", "start_line": 10, "end_line": 20}],
            "hypothesis_ready": True,
            "targeted_evidence_count": 1,
        },
        "agent_events": [],
        "commands": [],
    }


def test_duplicate_semantic_rejection_is_blocked_until_acceptance_state_changes():
    task = _task()
    cw = _CW(task)
    agent = _Agent(cw)
    guarded = _Guarded()

    hardening.install(agent, guarded, cw, _WorkPhases)

    first = guarded._run_tool_with_semantic_acceptance(
        "code-test", "coding_finish", {}, git_token_value=None
    )
    second = guarded._run_tool_with_semantic_acceptance(
        "code-test", "coding_finish", {}, git_token_value=None
    )

    assert first["error"] == "semantic_acceptance_rejected"
    assert second["error"] == "semantic_acceptance_state_unchanged"
    assert guarded.calls == 1
    assert agent._run_tool is guarded._run_tool_with_semantic_acceptance
    assert any(event["type"] == "semantic_acceptance_repeat_blocked" for event in agent.events)

    task["project_plan"] = {
        "revision": 3,
        "note": "Root cause: the payload-none early return skips InvokeAI management metadata.",
        "items": [],
    }
    third = guarded._run_tool_with_semantic_acceptance(
        "code-test", "coding_finish", {}, git_token_value=None
    )

    assert third["error"] == "semantic_acceptance_rejected"
    assert guarded.calls == 2


def test_validation_provenance_ignores_later_git_inspection_command():
    task = _task()
    task["commands"] = [
        {
            "label": "agent-command",
            "ts": 11.0,
            "argv": ["python3", "-m", "py_compile", "app.py"],
            "ok": True,
        },
        {
            "label": "agent-command",
            "ts": 12.0,
            "argv": ["git", "log", "--oneline", "-20", "--", "app.py"],
            "ok": True,
        },
    ]
    snapshot = {
        "changes": {"last_edit_at": 10.0},
        "validation": {
            "last_validation_command": ["git", "log", "--oneline", "-20", "--", "app.py"],
            "last_validation_ok": True,
            "last_validation_at": 12.0,
            "validation_after_latest_edit": True,
        },
    }

    validation = hardening._reconciled_validation_state(
        task,
        snapshot,
        is_validation_command=_WorkPhases.is_validation_command,
    )

    assert validation["last_validation_command"] == ["python3", "-m", "py_compile", "app.py"]
    assert validation["last_validation_at"] == 11.0
    assert validation["last_validation_ok"] is True
    assert validation["validation_after_latest_edit"] is True


def test_nonvalidation_command_cannot_mint_post_edit_validation():
    task = _task()
    task["commands"] = [
        {
            "label": "agent-command",
            "ts": 12.0,
            "argv": ["git", "log", "--oneline", "-20", "--", "app.py"],
            "ok": True,
        }
    ]
    snapshot = {"changes": {"last_edit_at": 10.0}}

    validation = hardening._reconciled_validation_state(
        task,
        snapshot,
        is_validation_command=_WorkPhases.is_validation_command,
    )

    assert validation["last_validation_command"] == []
    assert validation["last_validation_ok"] is None
    assert validation["validation_after_latest_edit"] is False


def test_terminal_status_watch_tracks_sentinel_recoverable_states():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "coding_terminal_status_watch.js"
    ).read_text(encoding="utf-8")

    assert 'RECOVERABLE = new Set(["failed", "paused", "interrupted", "stopped"])' in source
    assert 'ACTIVE = new Set(["queued", "running", "stopping", "pausing"])' in source
    assert 'FINAL = new Set(["completed"])' in source
    assert "SUPERVISORY_POLL_MS = 15000" in source
    assert "GRACE_MS" not in source
    assert "TASK_ID_RE" in source
    assert 'document.querySelector(".task-item.active")' in source
    assert 'fetch(`/ui/api/coding/tasks/${encodeURIComponent(taskId)}`' in source
    assert "serverStatus === status" in source
    assert "refreshBtn.click()" in source
    assert "MutationObserver" in source
    assert "document.visibilityState" in source


def test_terminal_status_watch_is_injected_once_before_main_coding_script():
    html = '<body><script src="/static/coding.js?v=15"></script></body>'

    once = coding_routes_guarded._inject_debug_report_script(html)
    twice = coding_routes_guarded._inject_debug_report_script(once)

    assert once == twice
    assert once.count("coding_terminal_status_watch.js") == 1
    assert once.index("coding_terminal_status_watch.js") < once.index("coding.js?v=15")
