from __future__ import annotations

from types import SimpleNamespace

from app import coding_forced_action as forced
from app import coding_hypothesis_persistence as persistence
from app.models import ToolFunction, ToolSpec


TARGET = "services/gateway/app/ui_routes.py"


def _state() -> dict:
    return {
        "action_kind": "evidence",
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_evidence_linked": False,
        "allowed_tools": ["coding_finish", "coding_update_plan"],
        "required_action": "Record a verified remediation hypothesis.",
    }


def _valid_note(repository_evidence: str = TARGET) -> str:
    return (
        "Root cause: model-management metadata is coupled to catalog discovery success.\n"
        f"Repository evidence: {repository_evidence} returns before management metadata is populated.\n"
        "Competing explanation checked: the frontend renderer and management target still exist.\n"
        "Expected result: the configured InvokeAI management URL remains available when catalog discovery fails."
    )


class FakeForcedAction:
    def __init__(self, state: dict):
        self.state = dict(state)
        self._execution_provenance_base = forced

    def active_state(self, task):
        return dict(self.state)


class FakeWorkspace:
    def __init__(self):
        self.task = {
            "id": "code_test",
            "project_plan": {
                "revision": 0,
                "goal": "Restore the InvokeAI link",
                "items": [],
                "note": "",
            },
        }

    def load_task(self, task_id):
        assert task_id == "code_test"
        return self.task


def _fake_agent(*, promote_after_write: bool = True):
    state = _state()
    forced_action = FakeForcedAction(state)
    workspace = FakeWorkspace()
    calls = []

    def specs_for_task(task):
        return [
            ToolSpec(
                function=ToolFunction(
                    name="coding_update_plan",
                    description="General project-plan editor.",
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
                    name="coding_finish",
                    description="Finish.",
                    parameters={"type": "object", "properties": {}},
                )
            ),
        ]

    def run_tool(task_id, name, args, *, git_token_value):
        calls.append((name, dict(args)))
        if name != "coding_update_plan":
            return {"ok": True}
        workspace.task["project_plan"] = {
            **workspace.task["project_plan"],
            "revision": 1,
            "note": str(args.get("note") or ""),
        }
        if promote_after_write:
            forced_action.state.update(
                {
                    "action_kind": "edit",
                    "hypothesis_ready": True,
                    "hypothesis_causal_evidence_linked": True,
                    "hypothesis_fields": list(forced._HYPOTHESIS_FIELDS),
                    "hypothesis_causal_targets": [TARGET],
                    "allowed_tools": ["coding_apply_patch", "coding_finish"],
                }
            )
        return {"ok": True, "plan": workspace.task["project_plan"]}

    agent = SimpleNamespace(
        ToolFunction=ToolFunction,
        ToolSpec=ToolSpec,
        forced_action=forced_action,
        cw=workspace,
        _tool_specs_for_task=specs_for_task,
        _run_tool=run_tool,
    )
    evidence_policy = SimpleNamespace(
        _provenance_prompt_context=lambda base, current: "base provenance prompt",
        _repository_evidence_links_target=(
            lambda repository_evidence, target: target.casefold()
            in str(repository_evidence or "").casefold().replace("\\", "/")
        ),
    )
    persistence.install(agent, evidence_policy)
    return agent, evidence_policy, forced_action, workspace, calls


def test_forced_hypothesis_tool_schema_is_note_only_and_names_verified_target():
    agent, _, _, _, _ = _fake_agent()

    specs = agent._tool_specs_for_task({})
    update = next(spec for spec in specs if spec.function.name == "coding_update_plan")
    params = update.function.parameters

    assert params["required"] == ["note"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"note"}
    assert TARGET in update.function.description
    assert "assistant prose does not count" in update.function.description
    assert "Root cause" in params["properties"]["note"]["description"]
    assert "Repository evidence" in params["properties"]["note"]["description"]


def test_production_shape_goal_items_without_note_is_rejected_before_plan_mutation():
    agent, _, _, workspace, calls = _fake_agent()
    before = dict(workspace.task["project_plan"])

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {
            "goal": "Restore the InvokeAI backend link in the Image UI",
            "items": [
                {
                    "id": "restore_invokeai_link",
                    "title": "Restore InvokeAI backend link in Image UI",
                    "status": "in_progress",
                    "summary": "PR #79 appears to have removed the link",
                }
            ],
        },
        git_token_value=None,
    )

    assert result["ok"] is False
    assert result["error"] == persistence._ERROR_REQUIRED
    assert "note argument" in result["message"]
    assert set(result["missing_hypothesis_fields"]) == set(forced._HYPOTHESIS_FIELDS)
    assert result["verified_causal_targets"] == [TARGET]
    assert workspace.task["project_plan"] == before
    assert calls == []


def test_hypothesis_note_must_link_exact_verified_repository_evidence():
    agent, _, _, workspace, calls = _fake_agent()
    before = dict(workspace.task["project_plan"])

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {"note": _valid_note("the recent PR #79 merge")},
        git_token_value=None,
    )

    assert result["ok"] is False
    assert result["error"] == persistence._ERROR_UNLINKED
    assert result["verified_causal_targets"] == [TARGET]
    assert workspace.task["project_plan"] == before
    assert calls == []


def test_valid_hypothesis_is_canonicalized_persisted_and_unlocks_edit_state():
    agent, _, forced_action, workspace, calls = _fake_agent()

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {"note": _valid_note()},
        git_token_value=None,
    )

    assert result["ok"] is True
    assert result["hypothesis_persisted"] is True
    assert result["hypothesis_causal_targets"] == [TARGET]
    assert result["next_action_kind"] == "edit"
    assert "coding_apply_patch" in result["next_allowed_tools"]
    assert len(calls) == 1
    assert set(calls[0][1]) == {"note"}
    persisted = workspace.task["project_plan"]["note"]
    assert persisted.splitlines()[0].startswith("Root cause:")
    assert persisted.splitlines()[1].startswith("Repository evidence:")
    assert persisted.splitlines()[2].startswith("Competing explanation checked:")
    assert persisted.splitlines()[3].startswith("Expected result:")
    assert forced_action.state["hypothesis_causal_evidence_linked"] is True


def test_tool_reports_failure_when_durable_re_read_does_not_recognize_hypothesis():
    agent, _, _, workspace, calls = _fake_agent(promote_after_write=False)

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {"note": _valid_note()},
        git_token_value=None,
    )

    assert len(calls) == 1
    assert workspace.task["project_plan"]["note"]
    assert result["ok"] is False
    assert result["error"] == persistence._ERROR_NOT_PERSISTED
    assert "controller could not re-read the durable plan" in result["message"]


def test_prompt_explicitly_distinguishes_assistant_prose_from_durable_note():
    agent, evidence_policy, _, _, _ = _fake_agent()

    prompt = evidence_policy._provenance_prompt_context(forced, _state())

    assert "Do not merely state the remediation hypothesis in assistant prose" in prompt
    assert "ONLY its note argument" in prompt
    assert TARGET in prompt
    assert "Do not update goal, items, or milestone summaries" in prompt


def test_normal_plan_editing_schema_and_runtime_are_unchanged_without_contract_state():
    agent, _, forced_action, workspace, calls = _fake_agent()
    forced_action.state = {}

    specs = agent._tool_specs_for_task({})
    update = next(spec for spec in specs if spec.function.name == "coding_update_plan")
    assert set(update.function.parameters["properties"]) == {"goal", "items", "note"}
    assert "required" not in update.function.parameters

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {"goal": "ordinary planning update"},
        git_token_value=None,
    )

    assert result["ok"] is True
    assert calls == [("coding_update_plan", {"goal": "ordinary planning update"})]
    assert workspace.task["project_plan"]["revision"] == 1
