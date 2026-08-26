from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from app import coding_forced_action as base_forced_action
from app import coding_hypothesis_persistence as hypothesis_persistence
from app import coding_hypothesis_transition_hardening as hardening


class _ToolFunction:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ToolSpec:
    def __init__(self, *, function):
        self.function = function


class _ForcedAction:
    _HYPOTHESIS_FIELDS = base_forced_action._HYPOTHESIS_FIELDS
    _HYPOTHESIS_FIELD_RE = base_forced_action._HYPOTHESIS_FIELD_RE
    _ACTION_ALLOWED_TOOLS = base_forced_action._ACTION_ALLOWED_TOOLS

    def __init__(self, state):
        self.state = dict(state)

    def active_state(self, _task):
        return dict(self.state)

    @staticmethod
    def evaluate_tool_call(
        _task,
        *,
        name,
        args,
        is_validation_command,
    ):
        del name, args, is_validation_command
        return True, {}


class _CW:
    def __init__(self, task):
        self.task = task
        self.snapshot = {
            "changes": {"counts": {"total": 0}, "changed_files": []},
            "progress": {
                "current_phase": "editing",
                "next_recommended_action": "continue the current project-plan milestone",
            },
            "mission_acceptance": {
                "schema": "nexus_coding_mission_acceptance_epoch.v1",
                "status": "pending",
                "base_head": "base-head",
                "current_head": "base-head",
                "has_delta": False,
            },
        }

    def load_task(self, _task_id):
        return self.task

    def coding_state_snapshot(self, _task_id):
        return {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in self.snapshot.items()
        }


class _DebugReport:
    @staticmethod
    def redact_text(value, *, limit=4000):
        return str(value or "")[:limit]

    @staticmethod
    def _sanitize(value, **_kwargs):
        return value

    @staticmethod
    def _event_view(event):
        return {
            "type": event.get("type"),
            "name": event.get("name"),
        }

    @staticmethod
    def _durable_state_view(result):
        if not result.get("ok"):
            return {"ok": False}
        return {"ok": True, "progress": {}}


class _EvidencePolicy:
    @staticmethod
    def _repository_evidence_links_target(repository_evidence, target):
        return str(target) in str(repository_evidence)


def _state():
    return {
        "action_kind": "evidence",
        "canonical_action_kind": "edit",
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": [
            "services/gateway/app/static/image_catalog_ui.js",
            "services/gateway/app/ui_routes.py",
        ],
        "durable_hypothesis_note_ready": False,
        "allowed_tools": ["coding_finish", "coding_update_plan"],
        "required_action": (
            "The durable project_plan.note is empty. Persist the four-field remediation "
            "hypothesis with coding_update_plan.note before editing."
        ),
    }


def _fixture():
    task = {
        "id": "code-test",
        "project_plan": {"revision": 0, "items": [], "note": ""},
    }
    forced = _ForcedAction(_state())
    agent = SimpleNamespace(
        ToolFunction=_ToolFunction,
        ToolSpec=_ToolSpec,
        forced_action=forced,
    )
    generic_plan = _ToolSpec(
        function=_ToolFunction(
            name="coding_update_plan",
            description="Generic project plan update",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "items": {"type": "array"},
                    "note": {"type": "string"},
                },
            },
        )
    )
    finish = _ToolSpec(
        function=_ToolFunction(
            name="coding_finish",
            description="Finish",
            parameters={"type": "object", "properties": {}},
        )
    )
    refute = _ToolSpec(
        function=_ToolFunction(
            name="coding_refute_hypothesis",
            description="Refute",
            parameters={"type": "object", "properties": {}},
        )
    )
    agent._tool_specs_for_task = lambda _task: [generic_plan, finish, refute]
    cw = _CW(task)
    debug = _DebugReport()
    hardening.install(
        agent,
        cw,
        forced,
        hypothesis_persistence,
        _EvidencePolicy(),
        debug,
    )
    return agent, cw, forced, debug, task


def _valid_note():
    return "\n".join(
        [
            "Root cause: Model catalog discovery can return before InvokeAI management metadata is attached.",
            "Repository evidence: services/gateway/app/ui_routes.py lines 1640-1699 show the management metadata construction.",
            "Competing explanation checked: The frontend management link renderer still consumes management.ui_url.",
            "Expected result: InvokeAI management navigation remains available when model-list discovery fails.",
        ]
    )


def test_late_overlay_preserves_refutation_tool_and_specializes_update_plan_schema():
    agent, _cw, _forced, _debug, task = _fixture()
    specs = agent._tool_specs_for_task(task)
    names = [spec.function.name for spec in specs]
    assert "coding_refute_hypothesis" in names
    plan = next(spec for spec in specs if spec.function.name == "coding_update_plan")
    assert plan.function.parameters["required"] == ["note"]
    assert plan.function.parameters["additionalProperties"] is False
    assert set(plan.function.parameters["properties"]) == {"note"}


def test_malformed_hypothesis_update_is_a_forced_action_rejection_before_execution():
    _agent, _cw, forced, _debug, task = _fixture()
    allowed, rejection = forced.evaluate_tool_call(
        task,
        name="coding_update_plan",
        args={},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is False
    assert rejection["error"] == "coding_update_plan_hypothesis_required"
    assert rejection["policy_contract_rejection"] is True
    assert rejection["attempted_tool"] == "coding_update_plan"
    assert set(rejection["missing_hypothesis_fields"]) == set(base_forced_action._HYPOTHESIS_FIELDS)


def test_unlinked_hypothesis_update_is_rejected_but_valid_note_is_allowed():
    _agent, _cw, forced, _debug, task = _fixture()
    unlinked = _valid_note().replace(
        "services/gateway/app/ui_routes.py",
        "services/gateway/app/not_verified.py",
    )
    allowed, rejection = forced.evaluate_tool_call(
        task,
        name="coding_update_plan",
        args={"note": unlinked},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is False
    assert rejection["error"] == "coding_update_plan_hypothesis_unlinked"

    allowed, rejection = forced.evaluate_tool_call(
        task,
        name="coding_update_plan",
        args={"note": _valid_note()},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is True
    assert rejection == {}


def test_debug_event_exposes_only_safe_hypothesis_contract_result_metadata():
    _agent, _cw, _forced, debug, _task = _fixture()
    event = {
        "type": "tool_finished",
        "name": "coding_update_plan",
        "result": {
            "ok": False,
            "error": "coding_update_plan_hypothesis_required",
            "message": "Four fields are required.",
            "required_action": "Persist the four-field hypothesis.",
            "missing_hypothesis_fields": ["Root cause", "Repository evidence"],
            "note": "SECRET RAW PLAN CONTENT",
            "content": "SECRET SOURCE CONTENT",
        },
    }
    rendered = debug._event_view(event)
    assert rendered["result"]["error"] == "coding_update_plan_hypothesis_required"
    assert rendered["result"]["ok"] is False
    assert "missing_hypothesis_fields" in rendered["result"]
    assert "note" not in rendered["result"]
    assert "content" not in rendered["result"]


def test_debug_durable_state_keeps_mission_acceptance_section():
    _agent, cw, _forced, debug, _task = _fixture()
    state = cw.coding_state_snapshot("code-test")
    rendered = debug._durable_state_view({"ok": True, "value": state})
    assert rendered["mission_acceptance"]["base_head"] == "base-head"
    assert rendered["mission_acceptance"]["has_delta"] is False


def test_empty_plan_progress_surfaces_exact_forced_action_instead_of_fake_milestone():
    _agent, cw, _forced, _debug, _task = _fixture()
    state = cw.coding_state_snapshot("code-test")
    assert state["progress"]["current_phase"] == "editing"
    assert state["progress"]["next_recommended_action"].startswith(
        "The durable project_plan.note is empty."
    )
    assert "milestone" not in state["progress"]["next_recommended_action"]


def test_guarded_routes_install_composition_overlay_without_raw_tool_spec_rebind():
    source = Path("services/gateway/app/coding_routes_guarded.py").read_text(encoding="utf-8")
    assert "coding_hypothesis_transition_hardening.install(" in source
    assert "_mission_tool_specs_for_task" not in source
    assert "_tool_specs_for_task = _mission_tool_specs_for_task" not in source
