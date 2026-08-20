from __future__ import annotations

import json
import re
from types import SimpleNamespace

from app import coding_agent
from app import coding_forced_action as forced
from app import coding_hypothesis_persistence as persistence
from app import coding_hypothesis_range_contract as contract
from app.models import ToolFunction, ToolSpec


IMAGE = "services/gateway/app/static/image.html"
ROUTES = "services/gateway/app/ui_routes.py"


def _tool_spec(name: str) -> ToolSpec:
    return ToolSpec(
        function=ToolFunction(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
        )
    )


def _parser_agent():
    return SimpleNamespace(
        _tool_specs=lambda: [
            _tool_spec("coding_replace_text"),
            _tool_spec("coding_finish"),
        ],
        _text_tool_call=coding_agent._text_tool_call,
    )


def _call_parts(call: dict) -> tuple[str, dict]:
    fn = call["function"]
    return fn["name"], json.loads(fn["arguments"])


def test_bare_text_tool_call_is_recovered_when_unambiguous_and_trailing():
    agent = _parser_agent()
    content = (
        "I will make the focused edit now.\n"
        'coding_replace_text{"path":"a.py","old_text":"old","new_text":"new"}'
    )

    calls = contract._extract_bare_text_tool_calls(agent, content)

    assert len(calls) == 1
    name, args = _call_parts(calls[0])
    assert name == "coding_replace_text"
    assert args == {"path": "a.py", "old_text": "old", "new_text": "new"}


def test_bare_text_tool_recovery_rejects_ambiguous_or_trailing_prose():
    agent = _parser_agent()

    ambiguous = (
        'coding_finish{"summary":"blocked","success":false}\n'
        'coding_replace_text{"path":"a.py","old_text":"old","new_text":"new"}'
    )
    trailing = (
        'coding_replace_text{"path":"a.py","old_text":"old","new_text":"new"}\n'
        "and then I will validate"
    )

    assert contract._extract_bare_text_tool_calls(agent, ambiguous) == []
    assert contract._extract_bare_text_tool_calls(agent, trailing) == []


def test_noop_replace_is_explicitly_rejected():
    result = contract._noop_replace_error(
        {
            "path": "services/gateway/app/static/image_catalog_ui.js",
            "old_text": "function applyBackendSelection() {}",
            "new_text": "function applyBackendSelection() {}",
        }
    )

    assert result is not None
    assert result["ok"] is False
    assert result["error"] == contract._ERROR_NOOP_EDIT
    assert result["replacements"] == 0
    assert result["no_op"] is True


def _valid_note() -> str:
    return (
        "Root cause: the catalog entry loses management metadata before the frontend renders it.\n"
        f"Repository evidence: {IMAGE}:200-319 and {ROUTES}:3520-3599 show the relevant flow.\n"
        "Competing explanation checked: the model-management container and renderer both still exist.\n"
        "Expected result: InvokeAI management navigation remains visible in the Image UI."
    )


def _range_contract_agent(validation: dict):
    state = {
        "action_kind": "evidence",
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": [IMAGE, ROUTES],
        "causal_evidence_ranges": [
            {"path": IMAGE, "start_line": 200, "end_line": 314},
            {"path": ROUTES, "start_line": 3520, "end_line": 3599},
        ],
        "durable_hypothesis_note_ready": False,
        "allowed_tools": ["coding_finish", "coding_update_plan"],
        "required_action": "Persist the bounded remediation hypothesis.",
    }
    task = {"project_plan": {"revision": 0, "note": ""}}
    calls: list[tuple[str, dict]] = []

    forced_action = SimpleNamespace(
        active_state=lambda current: dict(state),
        _execution_provenance_base=forced,
    )

    def run_tool(task_id, name, args, *, git_token_value):
        calls.append((name, dict(args)))
        return {"ok": True}

    agent = SimpleNamespace(
        ToolFunction=ToolFunction,
        ToolSpec=ToolSpec,
        forced_action=forced_action,
        cw=SimpleNamespace(load_task=lambda task_id: task),
        _tool_specs=lambda: [
            _tool_spec("coding_update_plan"),
            _tool_spec("coding_replace_text"),
        ],
        _tool_specs_for_task=lambda current: [_tool_spec("coding_update_plan")],
        _extract_text_tool_calls=lambda content: [],
        _text_tool_call=coding_agent._text_tool_call,
        _run_tool=run_tool,
    )
    evidence_policy = SimpleNamespace(
        _repository_evidence_links_target=lambda evidence, target: target in str(evidence)
    )
    range_provenance = SimpleNamespace(
        validate_repository_evidence=lambda *args, **kwargs: dict(validation)
    )

    contract.install(
        agent,
        evidence_policy,
        range_provenance,
        persistence,
    )
    return agent, calls


def test_mixed_valid_and_invalid_bounded_citations_are_rejected_before_plan_write():
    agent, calls = _range_contract_agent(
        {
            "matched_targets": [ROUTES],
            "matched_ranges": [
                {"path": ROUTES, "start_line": 3520, "end_line": 3599}
            ],
            "missing_range_targets": [IMAGE],
            "verified_ranges": [
                {"path": IMAGE, "start_line": 200, "end_line": 314},
                {"path": ROUTES, "start_line": 3520, "end_line": 3599},
            ],
        }
    )

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {"note": _valid_note()},
        git_token_value=None,
    )

    assert result["ok"] is False
    assert result["error"] == contract._ERROR_RANGE_UNVERIFIED
    assert result["unverified_bounded_targets"] == [IMAGE]
    assert result["verified_causal_ranges"][0] == {
        "path": IMAGE,
        "start_line": 200,
        "end_line": 314,
    }
    assert calls == []


def test_all_named_bounded_targets_must_validate_before_plan_write():
    agent, calls = _range_contract_agent(
        {
            "matched_targets": [IMAGE, ROUTES],
            "matched_ranges": [
                {"path": IMAGE, "start_line": 200, "end_line": 314},
                {"path": ROUTES, "start_line": 3520, "end_line": 3599},
            ],
            "missing_range_targets": [],
            "verified_ranges": [
                {"path": IMAGE, "start_line": 200, "end_line": 314},
                {"path": ROUTES, "start_line": 3520, "end_line": 3599},
            ],
        }
    )

    result = agent._run_tool(
        "code_test",
        "coding_update_plan",
        {"note": _valid_note().replace(f"{IMAGE}:200-319", f"{IMAGE}:200-314")},
        git_token_value=None,
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "coding_update_plan"


def test_noop_replace_is_rejected_before_workspace_mutation_in_installed_contract():
    agent, calls = _range_contract_agent(
        {
            "matched_targets": [IMAGE, ROUTES],
            "missing_range_targets": [],
            "verified_ranges": [],
        }
    )

    result = agent._run_tool(
        "code_test",
        "coding_replace_text",
        {"path": "a.py", "old_text": "same", "new_text": "same"},
        git_token_value=None,
    )

    assert result["ok"] is False
    assert result["error"] == contract._ERROR_NOOP_EDIT
    assert calls == []
