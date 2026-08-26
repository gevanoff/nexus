from __future__ import annotations

from types import SimpleNamespace

from app import coding_forced_action as base_forced_action
from app import coding_hypothesis_persistence as persistence
from app import coding_hypothesis_transition_hardening as hardening


class _ForcedAction:
    _HYPOTHESIS_FIELDS = base_forced_action._HYPOTHESIS_FIELDS
    _HYPOTHESIS_FIELD_RE = base_forced_action._HYPOTHESIS_FIELD_RE
    _ACTION_ALLOWED_TOOLS = base_forced_action._ACTION_ALLOWED_TOOLS

    def __init__(self, state):
        self.state = dict(state)

    def active_state(self, _task):
        return dict(self.state)

    @staticmethod
    def filter_tool_specs(specs, _task):
        return list(specs)

    @staticmethod
    def evaluate_tool_call(_task, *, name, args, is_validation_command):
        del name, args, is_validation_command
        return True, {}


class _EvidencePolicy:
    @staticmethod
    def _repository_evidence_links_target(repository_evidence, target):
        return str(target).casefold() in str(repository_evidence).casefold()

    @staticmethod
    def _evidence_window_start(_events, _state):
        return 0


class _ToolFunction:
    def __init__(self, *, name, description="", parameters=None):
        self.name = name
        self.description = description
        self.parameters = parameters or {}


class _ToolSpec:
    def __init__(self, *, function):
        self.function = function


class _CW:
    def __init__(self, task):
        self.task = dict(task)
        self.snapshot = {"changes": {"counts": {"total": 0}}, "progress": {}}

    def load_task(self, _task_id):
        return dict(self.task)

    def coding_state_snapshot(self, _task_id):
        return dict(self.snapshot)


class _Debug:
    @staticmethod
    def redact_text(value, *, limit=4000):
        return str(value or "")[:limit]

    @staticmethod
    def _sanitize(value):
        return value

    @staticmethod
    def _event_view(_event):
        return {}

    @staticmethod
    def _durable_state_view(result):
        return {"ok": bool(result.get("ok"))}


def _state():
    return {
        "action_kind": "evidence",
        "allowed_tools": ["coding_update_plan", "coding_finish"],
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": ["services/gateway/app/ui_routes.py"],
        "causal_evidence_ranges": [
            {
                "path": "services/gateway/app/ui_routes.py",
                "start_line": 10,
                "end_line": 20,
            }
        ],
        "durable_hypothesis_note_ready": False,
        "required_action": "Persist the four-field remediation hypothesis.",
    }


def _task():
    return {
        "project_plan": {"revision": 0, "items": [], "note": ""},
        "agent_events": [
            {
                "type": "tool_finished",
                "name": "coding_read_file_lines",
                "ts": 2.0,
                "result": {
                    "path": "services/gateway/app/ui_routes.py",
                    "start_line": 10,
                    "end_line": 20,
                    "content": "verified repository excerpt",
                },
            }
        ],
    }


def _note(repository_evidence):
    return "\n".join(
        [
            "Root cause: Catalog discovery returns before management metadata is attached.",
            f"Repository evidence: {repository_evidence}",
            "Competing explanation checked: Frontend management link rendering still exists.",
            "Expected result: InvokeAI management metadata remains available when catalog discovery fails.",
        ]
    )


def _agent(forced):
    generic = _ToolSpec(
        function=_ToolFunction(
            name="coding_update_plan",
            description="Generic plan update.",
            parameters={
                "type": "object",
                "properties": {"note": {"type": "string"}},
            },
        )
    )
    agent = SimpleNamespace()
    agent.forced_action = forced
    agent.ToolFunction = _ToolFunction
    agent.ToolSpec = _ToolSpec
    agent._tool_specs_for_task = lambda _task: [generic]
    agent._tool_specs = lambda: [generic]
    return agent


def test_late_schema_preserves_verified_range_guidance():
    task = _task()
    forced = _ForcedAction(_state())
    agent = _agent(forced)
    cw = _CW(task)
    hardening.install(agent, cw, forced, persistence, _EvidencePolicy(), _Debug())

    specs = agent._tool_specs_for_task(task)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.function.parameters["required"] == ["note"]
    assert spec.function.parameters["additionalProperties"] is False
    rendered = (
        str(spec.function.description)
        + "\n"
        + str(spec.function.parameters["properties"]["note"]["description"])
    )
    assert "services/gateway/app/ui_routes.py:10-20" in rendered


def test_range_mistake_is_rejected_during_policy_preflight():
    task = _task()
    forced = _ForcedAction(_state())
    agent = _agent(forced)
    cw = _CW(task)
    hardening.install(agent, cw, forced, persistence, _EvidencePolicy(), _Debug())

    allowed, rejection = forced.evaluate_tool_call(
        task,
        name="coding_update_plan",
        args={"note": _note("services/gateway/app/ui_routes.py")},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is False
    assert rejection["error"] == "coding_update_plan_hypothesis_range_unverified"
    assert rejection["policy_contract_rejection"] is True
    assert rejection["unverified_bounded_targets"] == ["services/gateway/app/ui_routes.py"]

    allowed, rejection = forced.evaluate_tool_call(
        task,
        name="coding_update_plan",
        args={"note": _note("services/gateway/app/ui_routes.py:10-20")},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is True
    assert rejection == {}
