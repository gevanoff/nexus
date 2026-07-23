from __future__ import annotations

from types import SimpleNamespace

from app import coding_runtime_guardrails as guards


def test_archive_stop_diagnostics_selects_run_nearest_archive_time():
    task = {
        "agent_status": "paused",
        "agent_runs": [
            {"run_id": "old", "status": "failed", "finished_at": 100, "error": "old error"},
            {"run_id": "target", "status": "paused", "finished_at": 200, "cycle": 202, "summary": "Coding run paused after reaching its wall-clock budget at cycle 202."},
            {"run_id": "later", "status": "completed", "finished_at": 400, "summary": "done"},
        ],
    }
    result = guards.archive_stop_diagnostics(task, {"archived_at": 250})
    assert result["run_id"] == "target"
    assert result["status"] == "paused"
    assert result["reason_code"] == "wall_clock_budget"
    assert result["cycle"] == 202
    assert "narrow" in result["remediation"].lower()


def test_archive_stop_diagnostics_reports_upstream_error_and_remediation():
    task = {
        "agent_runs": [
            {
                "run_id": "failed-run",
                "status": "failed",
                "finished_at": 100,
                "error": "{'upstream': 'local_mlx', 'status': 500, 'body': 'internal_error'}",
            }
        ]
    }
    result = guards.archive_stop_diagnostics(task, {"archived_at": 110})
    assert result["reason_code"] == "upstream_internal_error"
    assert "500" in result["error"]
    assert "backend logs" in result["remediation"].lower()


def test_archive_stop_diagnostics_prefers_recent_no_progress_limit():
    task = {
        "agent_status": "paused",
        "agent_runs": [{"run_id": "run", "status": "paused", "finished_at": 102, "summary": "Coding run was paused."}],
        "agent_events": [
            {"type": "no_progress_limit", "ts": 100, "summary": "Coding run paused after 8 tool actions without substantive progress."},
            {"type": "paused", "ts": 102, "summary": "Coding run was paused."},
        ],
    }
    result = guards.archive_stop_diagnostics(task, {"archived_at": 110})
    assert result["reason_code"] == "no_progress_limit"
    assert "substantive progress" in result["summary"]


def test_substantive_tool_result_rejects_reads_and_accepts_real_edits():
    ca = SimpleNamespace(_is_validation_command=lambda argv: bool(argv and "pytest" in argv))
    before = {"project_plan": {"revision": 1}}
    after = {"project_plan": {"revision": 1}}
    assert guards._substantive_tool_result("coding_read_file_lines", {}, {"ok": True}, before=before, after=after, ca=ca) is False
    assert guards._substantive_tool_result("coding_replace_text", {}, {"ok": True, "replacements": 1}, before=before, after=after, ca=ca) is True
    assert guards._substantive_tool_result("coding_replace_text", {}, {"ok": True, "replacements": 0}, before=before, after=after, ca=ca) is False
    assert guards._substantive_tool_result("coding_run_command", {"argv": ["pytest"]}, {"ok": False}, before=before, after=after, ca=ca) is True


def test_substantive_plan_update_requires_revision_change():
    ca = SimpleNamespace(_is_validation_command=lambda argv: False)
    before = {"project_plan": {"revision": 1}}
    unchanged = {"project_plan": {"revision": 1}}
    changed = {"project_plan": {"revision": 2}}
    assert guards._substantive_tool_result("coding_update_plan", {}, {"ok": True}, before=before, after=unchanged, ca=ca) is False
    assert guards._substantive_tool_result("coding_update_plan", {}, {"ok": True}, before=before, after=changed, ca=ca) is True


def test_repeated_validation_without_an_edit_is_not_new_progress():
    ca = SimpleNamespace(_is_validation_command=lambda argv: True)
    args = {"argv": ["pytest", "-q"]}
    signature = '["pytest","-q"]'
    before = {"project_plan": {"revision": 1}, "agent_no_progress_last_validation_signature": signature}
    after = {"project_plan": {"revision": 1}}
    assert guards._substantive_tool_result("coding_run_command", args, {"ok": True}, before=before, after=after, ca=ca) is False


def test_no_progress_outcome_requests_pause_at_configured_limit():
    task = {
        "agent_no_progress_cycles": 1,
        "agent_no_progress_updated_at": 10,
        "last_guidance_at": 5,
        "project_plan": {"revision": 0},
        "mission": {"budget_policy": {"max_no_progress_cycles": 2}},
    }
    events = []

    class FakeWorkspace:
        @staticmethod
        def load_task(_task_id):
            return task

        @staticmethod
        def normalize_coding_mission(value):
            return value["mission"]

        @staticmethod
        def mutate_task(_task_id, mutator):
            mutator(task)
            return task

    fake_agent = SimpleNamespace(
        _is_validation_command=lambda argv: False,
        _append_event=lambda task_id, event: events.append(event),
    )
    guards._record_tool_outcome(
        "code_test",
        "coding_search_text",
        {"query": "agent_status"},
        {"ok": True},
        before=dict(task),
        cw=FakeWorkspace,
        ca=fake_agent,
    )
    assert task["agent_no_progress_cycles"] == 2
    assert task["agent_pause_requested"] is True
    assert "without substantive progress" in task["agent_pause_reason"]
    assert events[-1]["type"] == "no_progress_limit"


def test_new_guidance_resets_persisted_no_progress_streak():
    task = {
        "agent_no_progress_cycles": 7,
        "agent_no_progress_updated_at": 10,
        "last_guidance_at": 20,
        "project_plan": {"revision": 0},
        "mission": {"budget_policy": {"max_no_progress_cycles": 8}},
    }
    events = []

    class FakeWorkspace:
        @staticmethod
        def load_task(_task_id):
            return task

        @staticmethod
        def normalize_coding_mission(value):
            return value["mission"]

        @staticmethod
        def mutate_task(_task_id, mutator):
            mutator(task)
            return task

    fake_agent = SimpleNamespace(
        _is_validation_command=lambda argv: False,
        _append_event=lambda task_id, event: events.append(event),
    )
    guards._record_tool_outcome(
        "code_test",
        "coding_read_file_lines",
        {"path": "x.py"},
        {"ok": True},
        before=dict(task),
        cw=FakeWorkspace,
        ca=fake_agent,
    )
    assert task["agent_no_progress_cycles"] == 1
    assert not task.get("agent_pause_requested")
    assert events[-1]["type"] == "no_progress_cycle"
