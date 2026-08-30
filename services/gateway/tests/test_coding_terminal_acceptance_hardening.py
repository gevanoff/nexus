from __future__ import annotations

from pathlib import Path

from app import coding_routes_guarded
from app import coding_terminal_acceptance_hardening as hardening


PY_COMPILE = ["python3", "-m", "py_compile", "app.py"]


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
        self.command_ts = 20.0
        self.serialization_requests = []
        self.coding_state_snapshot = lambda _task_id: {
            "changes": {"last_edit_at": 10.0},
            "validation": {
                "last_validation_command": ["git", "log", "--oneline"],
                "last_validation_ok": True,
                "last_validation_at": 20.0,
                "validation_after_latest_edit": True,
            },
            "progress": {
                "current_phase": "finalizing",
                "next_recommended_action": "finish the mission",
            },
            "diff_review": {
                "last_diff_review_at": 20.0,
                "diff_reviewed_after_latest_edit": True,
            },
        }

    def ensure_task_workspace_serialized(self, operation_name):
        self.serialization_requests.append(str(operation_name))
        return getattr(self, str(operation_name))

    def load_task(self, task_id):
        return self.tasks[task_id]

    def save_task(self, task):
        self.tasks[task["id"]] = task
        return task

    def run_task_command(self, task_id, *, argv, cwd=None, timeout_sec=None, git_token_value=None):
        del timeout_sec, git_token_value
        self.command_ts += 1.0
        result = {
            "ok": True,
            "argv": list(argv),
            "cwd": str(cwd or ""),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        task = self.tasks[task_id]
        commands = list(task.get("commands") or [])
        commands.append(
            {
                "label": "command",
                "ts": self.command_ts,
                "argv": list(argv),
                "ok": True,
            }
        )
        task["commands"] = commands[-30:]
        return result


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
        return list(argv or []) in (
            PY_COMPILE,
            ["pytest", "-q"],
        )


def _task():
    return {
        "id": "code-test",
        "agent_run_id": "coderun-test",
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


def test_validation_provenance_wrapper_reapplies_workspace_serialization():
    task = _task()
    cw = _CW(task)
    agent = _Agent(cw)
    guarded = _Guarded()

    hardening.install(agent, guarded, cw, _WorkPhases)

    assert cw.serialization_requests == ["run_task_command"]


def test_validation_provenance_ignores_later_git_inspection_command():
    task = _task()
    task["commands"] = [
        {
            "label": "agent-command",
            "ts": 11.0,
            "argv": PY_COMPILE,
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

    assert validation["last_validation_command"] == PY_COMPILE
    assert validation["last_validation_at"] == 11.0
    assert validation["last_validation_ok"] is True
    assert validation["validation_after_latest_edit"] is True
    assert validation["provenance_source"] == "command_ledger"


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
    assert validation["provenance_source"] == "none"


def test_validation_provenance_survives_truncated_command_ledger():
    task = _task()
    cw = _CW(task)
    agent = _Agent(cw)
    guarded = _Guarded()
    hardening.install(agent, guarded, cw, _WorkPhases)

    result = cw.run_task_command("code-test", argv=PY_COMPILE, cwd="")
    assert result["ok"] is True
    durable = task[hardening._VALIDATION_KEY]
    assert durable["schema"] == hardening._VALIDATION_SCHEMA
    assert durable["argv"] == PY_COMPILE
    assert durable["ok"] is True

    # Simulate the production 30-entry command ring buffer evicting the
    # validation while subsequent inspection commands continue to accumulate.
    task["commands"] = [
        {
            "label": "command",
            "ts": durable["ts"] + index + 1,
            "argv": ["git", "log", "--oneline", str(index)],
            "ok": True,
        }
        for index in range(30)
    ]

    snapshot = cw.coding_state_snapshot("code-test")
    assert snapshot["validation"]["last_validation_command"] == PY_COMPILE
    assert snapshot["validation"]["last_validation_ok"] is True
    assert snapshot["validation"]["validation_after_latest_edit"] is True
    assert snapshot["validation"]["provenance_source"] == "durable_provenance"


def test_reconciled_progress_does_not_finish_when_validation_is_missing():
    snapshot = {
        "changes": {
            "last_edit_at": 10.0,
            "changed_files": [{"path": "app.py", "status": " M"}],
        },
        "validation": {
            "last_validation_command": ["git", "log", "--oneline"],
            "last_validation_ok": True,
            "last_validation_at": 20.0,
            "validation_after_latest_edit": True,
        },
        "diff_review": {
            "last_diff_review_at": 20.0,
            "diff_reviewed_after_latest_edit": True,
        },
        "progress": {
            "current_phase": "finalizing",
            "next_recommended_action": "finish the mission",
        },
    }
    validation = {
        "last_validation_command": [],
        "last_validation_ok": None,
        "last_validation_at": 0.0,
        "validation_after_latest_edit": False,
    }

    progress = hardening._reconciled_progress_state(snapshot, validation)

    assert progress["current_phase"] == "editing"
    assert progress["next_recommended_action"] == "validate changes"


def test_reconciled_progress_requires_failed_validation_remediation():
    snapshot = {
        "changes": {
            "last_edit_at": 10.0,
            "changed_files": [{"path": "app.py", "status": " M"}],
        },
        "diff_review": {
            "last_diff_review_at": 20.0,
            "diff_reviewed_after_latest_edit": True,
        },
        "progress": {
            "current_phase": "finalizing",
            "next_recommended_action": "finish the mission",
        },
    }
    validation = {
        "last_validation_command": ["pytest", "-q"],
        "last_validation_ok": False,
        "last_validation_at": 20.0,
        "validation_after_latest_edit": True,
    }

    progress = hardening._reconciled_progress_state(snapshot, validation)

    assert progress["current_phase"] == "editing"
    assert progress["next_recommended_action"] == "resolve failed validation"


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
