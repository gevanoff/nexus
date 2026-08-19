from __future__ import annotations

from types import SimpleNamespace

from app import coding_forced_action as forced
from app import coding_hypothesis_persistence as persistence
from app.models import ToolFunction, ToolSpec


TARGET = "services/gateway/app/ui_routes.py"


def _valid_note() -> str:
    return (
        "Root cause: model-management metadata is coupled to catalog discovery success.\n"
        f"Repository evidence: {TARGET} returns before management metadata is populated.\n"
        "Competing explanation checked: the frontend renderer and management target still exist.\n"
        "Expected result: the InvokeAI management URL remains available when catalog discovery fails."
    )


def _precomputed_edit_state() -> dict:
    return {
        "action_kind": "edit",
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": [TARGET],
        "hypothesis_ready": True,
        "hypothesis_causal_evidence_linked": True,
        "hypothesis_fields": list(forced._HYPOTHESIS_FIELDS),
        "hypothesis_causal_targets": [TARGET],
        "activation_plan_revision": 0,
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
    }


class FakeForcedAction:
    def __init__(self, state: dict):
        self.state = dict(state)
        self._execution_provenance_base = forced

    def active_state(self, task):
        return dict(self.state)

    def prompt_context(self, task):
        return "legacy prompt"


class FakeWorkspace:
    def __init__(self, plan: dict):
        self.task = {"id": "code_review", "project_plan": dict(plan)}

    def load_task(self, task_id):
        assert task_id == "code_review"
        return self.task


def _agent_for_plan(plan: dict):
    forced_action = FakeForcedAction(_precomputed_edit_state())
    workspace = FakeWorkspace(plan)

    def specs_for_task(task):
        state = forced_action.active_state(task)
        allowed = set(state.get("allowed_tools") or [])
        specs = [
            ToolSpec(
                function=ToolFunction(
                    name="coding_update_plan",
                    description="general plan update",
                    parameters={
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "items": {"type": "array"},
                            "note": {"type": "string"},
                        },
                    },
                )
            ),
            ToolSpec(
                function=ToolFunction(
                    name="coding_apply_patch",
                    description="edit",
                    parameters={"type": "object", "properties": {}},
                )
            ),
            ToolSpec(
                function=ToolFunction(
                    name="coding_finish",
                    description="finish",
                    parameters={"type": "object", "properties": {}},
                )
            ),
        ]
        # Mirror the production facade's dynamic filtering.
        return [spec for spec in specs if spec.function.name in allowed]

    agent = SimpleNamespace(
        ToolFunction=ToolFunction,
        ToolSpec=ToolSpec,
        forced_action=forced_action,
        cw=workspace,
        _tool_specs_for_task=specs_for_task,
        _run_tool=lambda *args, **kwargs: {"ok": True},
    )
    evidence_policy = SimpleNamespace(
        _provenance_prompt_context=lambda base, state: "legacy provenance prompt",
        _repository_evidence_links_target=(
            lambda repository_evidence, target: target.casefold()
            in str(repository_evidence or "").casefold().replace("\\", "/")
        ),
    )
    persistence.install(agent, evidence_policy)
    return agent, forced_action, workspace


def test_codex_p1_goal_and_items_cannot_authorize_edit_with_empty_note():
    whole_plan_labels = (
        "Root cause: catalog handling is wrong. "
        f"Repository evidence: {TARGET}. "
        "Competing explanation checked: frontend still renders management links. "
        "Expected result: keep management access visible."
    )
    agent, _, workspace = _agent_for_plan(
        {
            "revision": 1,
            "goal": whole_plan_labels,
            "items": [
                {
                    "id": "Root cause: goal/item text masquerades as hypothesis",
                    "title": whole_plan_labels,
                    "status": "in_progress",
                    "summary": whole_plan_labels,
                }
            ],
            "note": "",
        }
    )

    state = agent.forced_action.active_state(workspace.task)

    assert state["action_kind"] == "evidence"
    assert state["durable_hypothesis_note_ready"] is False
    assert state["hypothesis_ready"] is False
    assert state["hypothesis_causal_evidence_linked"] is False
    assert "coding_update_plan" in state["allowed_tools"]
    assert "coding_apply_patch" not in state["allowed_tools"]
    specs = agent._tool_specs_for_task(workspace.task)
    names = {spec.function.name for spec in specs}
    assert names == {"coding_update_plan", "coding_finish"}
    update = next(spec for spec in specs if spec.function.name == "coding_update_plan")
    assert update.function.parameters["required"] == ["note"]
    assert set(update.function.parameters["properties"]) == {"note"}


def test_valid_durable_note_preserves_precomputed_edit_authorization():
    agent, _, workspace = _agent_for_plan(
        {
            "revision": 1,
            "goal": "Restore the InvokeAI link",
            "items": [],
            "note": _valid_note(),
        }
    )

    state = agent.forced_action.active_state(workspace.task)

    assert state["action_kind"] == "edit"
    assert state["durable_hypothesis_note_ready"] is True
    assert state["hypothesis_ready"] is True
    assert state["hypothesis_causal_evidence_linked"] is True
    assert state["durable_hypothesis_note_causal_targets"] == [TARGET]


def test_durable_note_from_activation_revision_does_not_unlock_edit():
    agent, _, workspace = _agent_for_plan(
        {
            "revision": 0,
            "goal": "Restore the InvokeAI link",
            "items": [],
            "note": _valid_note(),
        }
    )

    state = agent.forced_action.active_state(workspace.task)

    assert state["action_kind"] == "evidence"
    assert state["durable_hypothesis_note_ready"] is False
    assert state["durable_hypothesis_note_revision_ready"] is False
    assert "must be reaffirmed" in state["required_action"]


def test_forced_note_schema_matches_durable_plan_storage_limit():
    agent, _, workspace = _agent_for_plan(
        {
            "revision": 1,
            "goal": "Restore the InvokeAI link",
            "items": [],
            "note": "",
        }
    )

    update = next(
        spec
        for spec in agent._tool_specs_for_task(workspace.task)
        if spec.function.name == "coding_update_plan"
    )

    assert update.function.parameters["properties"]["note"]["maxLength"] == 2000
