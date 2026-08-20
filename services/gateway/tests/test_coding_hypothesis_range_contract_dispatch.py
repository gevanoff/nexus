from __future__ import annotations

from types import SimpleNamespace

from app import coding_hypothesis_range_contract as range_contract


def test_range_contract_preserves_guarded_semantic_dispatch_chain():
    def original_run_tool(task_id, name, args, *, git_token_value):
        return {"ok": True}

    agent = SimpleNamespace(
        forced_action=SimpleNamespace(active_state=lambda task: {}),
        cw=SimpleNamespace(load_task=lambda task_id: {}),
        _tool_specs_for_task=lambda task: [],
        _run_tool=original_run_tool,
    )
    guarded = SimpleNamespace(_run_tool_with_semantic_acceptance=original_run_tool)
    persistence = SimpleNamespace(_contract_required=lambda state: False)
    range_provenance = SimpleNamespace()

    range_contract.install(
        agent,
        SimpleNamespace(),
        range_provenance,
        persistence,
        guarded,
    )

    assert agent._run_tool is not original_run_tool
    assert guarded._run_tool_with_semantic_acceptance is agent._run_tool
