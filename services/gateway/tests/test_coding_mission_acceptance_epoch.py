from __future__ import annotations

import hashlib
from pathlib import Path

from app import coding_mission_acceptance_epoch as epoch


class _ToolFunction:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ToolSpec:
    def __init__(self, *, function):
        self.function = function


class _Agent:
    ToolFunction = _ToolFunction
    ToolSpec = _ToolSpec

    def __init__(self, cw):
        self.cw = cw
        self._run_tool = None
        self.finalize_calls = []

    @staticmethod
    def _clip_text(text, limit):
        return str(text or "")[:limit]

    @staticmethod
    def _tool_specs():
        return []

    def _append_event(self, task_id, event):
        del task_id
        self.cw.task.setdefault("agent_events", []).append(dict(event))

    async def start_agent_run(self, task_id, *args, **kwargs):
        del args, kwargs
        return {"id": task_id, "status": "queued"}

    @staticmethod
    def _mission_requires_workspace_edits(_task):
        return True

    def finalize_successful_run(self, task_id, *, mission=None, **kwargs):
        self.finalize_calls.append((task_id, mission, kwargs))
        return {"ok": True, "final_commit": self.cw.current_head}


class _Guarded:
    def __init__(self, agent, cw):
        self._agent = agent
        self.cw = cw
        self._mission_acceptance_epoch_installed = False
        self._run_delta_diff = lambda _task_id, _task: "run-local-diff"
        self._run_tool_with_semantic_acceptance = self._run

    def _run(self, task_id, name, args, *, git_token_value):
        del args, git_token_value
        if name == "coding_finish":
            review_diff = epoch.mission_review_diff(
                self.cw, self._agent, task_id, self.cw.task
            )
            fingerprint = _TerminalHardening.semantic_acceptance_fingerprint(
                self.cw.task, diff_text=review_diff
            )
            self._agent._append_event(
                task_id,
                {
                    "type": "semantic_acceptance_review",
                    "cycle": int(self.cw.task.get("agent_cycle") or 0),
                    "accepted": True,
                    "fingerprint": fingerprint,
                },
            )
            return {
                "ok": True,
                "success": True,
                "_semantic_acceptance_review_identity": {
                    "fingerprint": fingerprint,
                    "cycle": int(self.cw.task.get("agent_cycle") or 0),
                },
            }
        return {"ok": True}


class _TerminalHardening:
    @staticmethod
    def semantic_acceptance_fingerprint(task, *, diff_text):
        note = str((task.get("project_plan") or {}).get("note") or "")
        return hashlib.sha256(f"{diff_text}\n{note}".encode("utf-8")).hexdigest()


class _ForcedAction:
    SCHEMA = "nexus_coding_forced_action.v1"
    _TARGETED_EVIDENCE_TOOLS = {"coding_search_text", "coding_read_file_lines"}
    _MAX_TARGETED_EVIDENCE_ACTIONS = 2

    @staticmethod
    def active_state(task):
        raw = task.get("agent_forced_action") or {}
        return dict(raw) if raw.get("status") == "active" else {}

    @classmethod
    def allowed_tool_names(cls, task):
        return set(cls.active_state(task).get("allowed_tools") or [])

    @classmethod
    def filter_tool_specs(cls, specs, task):
        allowed = cls.allowed_tool_names(task)
        return list(specs) if not allowed else [item for item in specs if item.function.name in allowed]

    @classmethod
    def evaluate_tool_call(cls, task, *, name, args, is_validation_command):
        del args, is_validation_command
        allowed = name in cls.allowed_tool_names(task)
        return (allowed, {} if allowed else {"error": "forced_action_tool_rejected"})

    @classmethod
    def prompt_context(cls, task):
        state = cls.active_state(task)
        return f"Action kind: {state.get('action_kind') or ''}" if state else ""

    @staticmethod
    def _targeted_evidence_result_succeeded(_name, result):
        return not result.get("error") and result.get("ok") is not False

    @staticmethod
    def _structured_hypothesis(task, state):
        revision = int((task.get("project_plan") or {}).get("revision") or 0)
        if revision <= int(state.get("activation_plan_revision") or 0):
            return False, {}
        return True, {
            "Root cause": "new cause",
            "Repository evidence": "new evidence",
            "Competing explanation checked": "checked",
            "Expected result": "fixed behavior",
        }


class _CW:
    def __init__(self, *, tracked_diff="diff --git a/app.py b/app.py\n+fixed\n"):
        self.current_head = "checkpoint-head"
        self.merge_base = "mission-base"
        self.tracked_diff = tracked_diff
        self.task = {
            "id": "code-test",
            "repo_path": "/repo",
            "base_branch": "main",
            "branch_name": "nexus-coder/code-test",
            "agent_run_id": "run-b",
            "agent_start_head": "checkpoint-head",
            "agent_cycle": 9,
            "agent_events": [],
            "project_plan": {
                "revision": 2,
                "note": "Root cause: inherited checkpoint still needs acceptance.",
                "items": [],
            },
            "coding_mission": {
                "completion_policy": {
                    "require_file_changes": True,
                    "require_commit_on_success": True,
                    "require_validation_after_edit": True,
                    "require_diff_review_after_edit": True,
                },
                "publish_policy": {},
            },
        }
        self.snapshot = {
            "changes": {"changed_files": [], "last_edit_at": 10.0},
            "validation": {
                "last_validation_command": ["pytest", "-q"],
                "last_validation_ok": True,
                "last_validation_at": 20.0,
                "validation_after_latest_edit": True,
            },
            "diff_review": {
                "last_diff_review_at": 21.0,
                "diff_reviewed_after_latest_edit": True,
            },
            "progress": {
                "current_phase": "editing",
                "next_recommended_action": "continue the current project-plan milestone",
            },
        }
        self.coding_state_snapshot = lambda _task_id: dict(self.snapshot)

    def load_task(self, _task_id):
        return self.task

    def save_task(self, task):
        self.task = task
        return task

    def mutate_task(self, _task_id, apply):
        apply(self.task)
        return self.task

    @staticmethod
    def _repo_path(_task):
        return Path("/repo")

    def _git_base_branch_diff(self, _repo, *, base_branch):
        assert base_branch == "main"
        return {"ok": True, "merge_base": self.merge_base, "compare_ref": self.merge_base}

    def _run_process(self, argv, *, cwd, timeout_sec=30.0):
        assert cwd == Path("/repo")
        del timeout_sec
        if argv[:2] == ["git", "rev-parse"]:
            candidate = argv[-1]
            if candidate == "HEAD":
                candidate = self.current_head
            return {"ok": True, "stdout": f"{candidate}\n", "stderr": ""}
        if argv[:2] == ["git", "diff"]:
            return {"ok": True, "stdout": self.tracked_diff, "stderr": ""}
        if argv[:3] == ["git", "ls-files", "--others"]:
            return {"ok": True, "stdout": "", "stderr": ""}
        return {"ok": False, "stdout": "", "stderr": f"unexpected argv: {argv}"}

    def git_head(self, _task_id):
        return {"ok": True, "commit": self.current_head}

    def normalize_coding_mission(self, task, mission=None):
        if mission is not None:
            return dict(mission)
        return dict(task.get("coding_mission") or {})


def test_checkpoint_commit_remains_inside_mission_delta_after_resume():
    cw = _CW()
    first = epoch.mission_delta_state(cw, "code-test", cw.task)
    assert first["ok"] is True
    assert first["has_delta"] is True
    assert first["base_head"] == "mission-base"
    assert first["current_head"] == "checkpoint-head"
    assert "fixed" in first["diff_text"]

    cw.task["agent_run_id"] = "run-c"
    cw.task["agent_start_head"] = "checkpoint-head"
    cw.merge_base = "newer-main-merge-base"
    resumed = epoch.mission_delta_state(cw, "code-test", cw.task)
    assert resumed["has_delta"] is True
    assert resumed["base_head"] == "mission-base"
    assert cw.task[epoch.KEY]["base_head"] == "mission-base"


def test_clean_worktree_with_inherited_delta_progresses_to_semantic_acceptance():
    cw = _CW()
    output = epoch._reconcile_snapshot(
        _TerminalHardening(),
        cw,
        _Agent(cw),
        "code-test",
        cw.task,
        cw.snapshot,
    )
    assert output["changes"]["changed_files"] == []
    assert output["mission_acceptance"]["has_delta"] is True
    assert output["progress"]["current_phase"] == "finalizing"
    assert output["progress"]["next_recommended_action"] == "finish the mission for semantic acceptance"


def test_accepted_inherited_delta_can_finalize_without_new_run_delta():
    cw = _CW()
    agent = _Agent(cw)
    guarded = _Guarded(agent, cw)
    forced = _ForcedAction()
    terminal = _TerminalHardening()
    epoch.install(agent, guarded, cw, forced, terminal)

    assert agent._mission_requires_workspace_edits(cw.task) is False
    finish = guarded._run_tool_with_semantic_acceptance(
        "code-test", "coding_finish", {}, git_token_value=None
    )
    assert finish["ok"] is True
    assert finish["success"] is True
    assert "_semantic_acceptance_review_identity" not in finish
    assert cw.task[epoch.KEY]["status"] == "semantic_accepted"
    assert cw.task[epoch.KEY]["accepted_fingerprint"]

    finalization = agent.finalize_successful_run(
        "code-test", mission=cw.task["coding_mission"], run_id="run-b"
    )
    assert finalization["ok"] is True
    patched_mission = agent.finalize_calls[-1][1]
    assert patched_mission["completion_policy"]["require_file_changes"] is False
    assert cw.task[epoch.KEY]["status"] == "finalized"


def test_unaccepted_inherited_delta_does_not_relax_finalization_contract():
    cw = _CW()
    agent = _Agent(cw)
    guarded = _Guarded(agent, cw)
    epoch.install(agent, guarded, cw, _ForcedAction(), _TerminalHardening())
    result = agent.finalize_successful_run(
        "code-test", mission=cw.task["coding_mission"], run_id="run-b"
    )
    assert result["ok"] is True
    original_mission = agent.finalize_calls[-1][1]
    assert original_mission["completion_policy"]["require_file_changes"] is True


def test_refutation_tool_does_not_widen_active_state_but_is_effectively_allowed():
    cw = _CW()
    cw.task["agent_forced_action"] = {
        "schema": _ForcedAction.SCHEMA,
        "status": "active",
        "state_key": "state-a",
        "action_kind": "edit",
        "required_action": "Make the smallest evidence-backed edit.",
        "allowed_tools": ["coding_apply_patch", "coding_finish"],
    }
    agent = _Agent(cw)
    guarded = _Guarded(agent, cw)
    forced = _ForcedAction()
    epoch.install(agent, guarded, cw, forced, _TerminalHardening())

    edit_state = forced.active_state(cw.task)
    assert epoch.REFUTATION_TOOL not in edit_state["allowed_tools"]
    assert "coding_update_plan" not in edit_state["allowed_tools"]
    assert epoch.REFUTATION_TOOL in forced.allowed_tool_names(cw.task)
    assert forced.evaluate_tool_call(
        cw.task,
        name=epoch.REFUTATION_TOOL,
        args={"reason": "contradicted"},
        is_validation_command=lambda _argv: False,
    )[0] is True

    result = guarded._run_tool_with_semantic_acceptance(
        "code-test",
        epoch.REFUTATION_TOOL,
        {
            "reason": "Verified frontend evidence contradicts the backend-only hypothesis.",
            "contradicting_evidence": "image_catalog_ui.js already renders management.ui_url.",
        },
        git_token_value=None,
    )
    assert result["ok"] is True
    assert result["refuted"] is True

    evidence_state = forced.active_state(cw.task)
    assert evidence_state["action_kind"] == "evidence"
    assert set(evidence_state["allowed_tools"]) == {
        "coding_search_text",
        "coding_read_file_lines",
        "coding_update_plan",
        "coding_finish",
    }

    refuted_at = float(cw.task[epoch.REFUTATION_KEY]["refuted_at"])
    cw.task["agent_events"].append(
        {
            "type": "tool_finished",
            "ts": refuted_at + 1.0,
            "name": "coding_read_file_lines",
            "result": {"path": "app.py", "content": "verified", "ok": True},
        }
    )
    cw.task["project_plan"] = {
        "revision": 3,
        "note": (
            "Root cause: replacement cause\n"
            "Repository evidence: app.py lines 1-20\n"
            "Competing explanation checked: old cause contradicted\n"
            "Expected result: corrected behavior"
        ),
    }
    replacement_edit = forced.active_state(cw.task)
    assert replacement_edit["action_kind"] == "edit"
    assert epoch.REFUTATION_TOOL not in replacement_edit["allowed_tools"]
    assert "coding_update_plan" not in replacement_edit["allowed_tools"]
    assert epoch.REFUTATION_TOOL in forced.allowed_tool_names(cw.task)
    assert replacement_edit["hypothesis_ready"] is True


def test_refutation_tool_is_hidden_without_forced_edit_state():
    cw = _CW()
    agent = _Agent(cw)
    guarded = _Guarded(agent, cw)
    forced = _ForcedAction()
    epoch.install(agent, guarded, cw, forced, _TerminalHardening())

    all_specs = agent._tool_specs()
    assert any(item.function.name == epoch.REFUTATION_TOOL for item in all_specs)
    visible = forced.filter_tool_specs(all_specs, cw.task)
    assert all(item.function.name != epoch.REFUTATION_TOOL for item in visible)
