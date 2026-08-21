from __future__ import annotations

from types import SimpleNamespace

from app import coding_evidence_range_provenance as range_provenance
from app import coding_forced_action as forced
from app import coding_hypothesis_persistence as persistence
from app import coding_hypothesis_range_contract as range_contract
from app import coding_verified_evidence_handoff as evidence_handoff


TARGET = "services/gateway/app/static/image.html"


def _events() -> list[dict]:
    return [
        {
            "ts": 10.0,
            "type": "tool_started",
            "tool_call_id": "read-eof",
            "name": "coding_read_file_lines",
            "args": {"path": TARGET, "start_line": 200, "line_count": 120},
        },
        {
            "ts": 11.0,
            "type": "tool_finished",
            "tool_call_id": "read-eof",
            "name": "coding_read_file_lines",
            "result": {
                "ok": True,
                "path": TARGET,
                "start_line": 200,
                "end_line": 314,
                "line_count": 115,
                "total_lines": 314,
                "truncated": False,
                "content": '<div id="modelManagement"></div>\n<script src="/static/image_catalog_ui.js?v=5"></script>',
            },
        },
    ]


def _state(*, action_kind: str = "evidence") -> dict:
    return {
        "action_kind": action_kind,
        "canonical_action_kind": "edit",
        "canonical_required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": [TARGET],
        "causal_evidence_ranges": [
            {"path": TARGET, "start_line": 200, "end_line": 314}
        ],
        "hypothesis_causal_targets": [TARGET] if action_kind == "edit" else [],
        "hypothesis_causal_evidence_linked": action_kind == "edit",
        "durable_hypothesis_note_ready": action_kind == "edit",
        "allowed_tools": (
            ["coding_apply_patch", "coding_finish", "coding_replace_text", "coding_write_file"]
            if action_kind == "edit"
            else ["coding_finish", "coding_update_plan"]
        ),
        "required_action": "Persist the range-qualified remediation hypothesis.",
    }


def _note(span: str) -> str:
    return (
        "Root cause: the Image UI management link disappears in the failing catalog path.\n"
        f"Repository evidence: {TARGET}:{span} contains the verified Image UI management surface.\n"
        "Competing explanation checked: the backend still exposes model-management metadata.\n"
        "Expected result: the range-qualified hypothesis unlocks the smallest evidence-backed edit."
    )


class _ToolFunction:
    def __init__(self, *, name: str, description: str, parameters: dict):
        self.name = name
        self.description = description
        self.parameters = parameters


class _ToolSpec:
    def __init__(self, *, function):
        self.function = function


class _ForcedAction:
    def __init__(self, state: dict):
        self.state = state
        self._execution_provenance_base = forced

    def active_state(self, task):
        return dict(self.state)


class _Workspace:
    def __init__(self):
        self.task = {
            "id": "code_eof",
            "agent_events": _events(),
            "project_plan": {"revision": 0, "goal": "Restore link", "items": [], "note": ""},
        }

    def load_task(self, task_id: str):
        assert task_id == "code_eof"
        return self.task


def _policy():
    return SimpleNamespace(
        _evidence_window_start=lambda events, state: 0,
        _repository_evidence_links_target=(
            lambda repository_evidence, target: target.casefold()
            in str(repository_evidence or "").casefold().replace("\\", "/")
        ),
    )


def _agent():
    state = _state()
    workspace = _Workspace()
    calls: list[tuple[str, dict]] = []

    def specs_for_task(task):
        return [
            _ToolSpec(
                function=_ToolFunction(
                    name="coding_update_plan",
                    description="Persist the structured remediation hypothesis.",
                    parameters={
                        "type": "object",
                        "required": ["note"],
                        "additionalProperties": False,
                        "properties": {"note": {"type": "string", "description": "Structured note."}},
                    },
                )
            )
        ]

    def run_tool(task_id, name, args, *, git_token_value):
        calls.append((name, dict(args)))
        return {"ok": True, "plan": {"note": str(args.get("note") or "")}}

    agent = SimpleNamespace(
        ToolFunction=_ToolFunction,
        ToolSpec=_ToolSpec,
        forced_action=_ForcedAction(state),
        cw=workspace,
        _tool_specs_for_task=specs_for_task,
        _run_tool=run_tool,
    )
    policy = _policy()
    range_contract.install(agent, policy, range_provenance, persistence)
    return agent, policy, workspace, calls


def test_eof_shortened_read_validates_only_actual_completed_span():
    task = {"agent_events": _events()}
    state = _state(action_kind="edit")
    policy = _policy()

    invalid = range_provenance.validate_repository_evidence(
        policy,
        forced,
        task,
        state,
        f"{TARGET}:200-319 requested read bounds",
        targets=[TARGET],
    )
    valid = range_provenance.validate_repository_evidence(
        policy,
        forced,
        task,
        state,
        f"{TARGET}:200-314 actual completed bounds",
        targets=[TARGET],
    )

    assert invalid["ok"] is False
    assert invalid["missing_range_targets"] == [TARGET]
    assert invalid["verified_ranges"] == [
        {"path": TARGET, "start_line": 200, "end_line": 314}
    ]
    assert valid["ok"] is True
    assert valid["matched_ranges"] == [
        {"path": TARGET, "start_line": 200, "end_line": 314}
    ]


def test_range_finalizer_uses_same_validator_for_eof_case():
    policy = _policy()
    state = _state(action_kind="edit")
    task = {
        "agent_events": _events(),
        "project_plan": {
            "revision": 1,
            "goal": "Restore link",
            "items": [],
            "note": _note("200-319"),
        },
    }

    rejected = range_provenance.refine_state(policy, forced, task, state)
    assert rejected["action_kind"] == "evidence"
    assert rejected["hypothesis_evidence_range_required"] is True
    assert rejected["causal_evidence_ranges"] == [
        {"path": TARGET, "start_line": 200, "end_line": 314}
    ]

    task["project_plan"]["note"] = _note("200-314")
    accepted = range_provenance.refine_state(policy, forced, task, state)
    assert accepted["action_kind"] == "edit"
    assert accepted["hypothesis_causal_evidence_ranges"] == [
        {"path": TARGET, "start_line": 200, "end_line": 314}
    ]


def test_plan_preflight_rejects_unverified_range_before_mutation():
    agent, _, workspace, calls = _agent()
    before = dict(workspace.task["project_plan"])

    result = agent._run_tool(
        "code_eof",
        "coding_update_plan",
        {"note": _note("200-319")},
        git_token_value=None,
    )

    assert result["ok"] is False
    assert result["error"] == range_contract._ERROR_RANGE_UNVERIFIED
    assert result["verified_causal_ranges"] == [
        {"path": TARGET, "start_line": 200, "end_line": 314}
    ]
    assert workspace.task["project_plan"] == before
    assert calls == []


def test_plan_preflight_accepts_actual_range_and_calls_persistence_layer():
    agent, _, _, calls = _agent()

    result = agent._run_tool(
        "code_eof",
        "coding_update_plan",
        {"note": _note("200-314")},
        git_token_value=None,
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "coding_update_plan"


def test_plan_tool_contract_names_authoritative_actual_span():
    agent, _, _, _ = _agent()

    spec = agent._tool_specs_for_task({})[0]

    assert f"{TARGET}:200-314" in spec.function.description
    assert f"{TARGET}:200-314" in spec.function.parameters["properties"]["note"]["description"]
    assert "actual completed span" in spec.function.description


def test_verified_evidence_handoff_surfaces_actual_completed_span():
    digest = evidence_handoff._verified_range_digest(_state())

    assert f"Verified repository span: {TARGET}:200-314" in digest
    assert "requested read bounds may be wider when EOF is reached" in digest
    assert "200-319" not in digest
