from __future__ import annotations

from app import coding_evidence_freshness as freshness


TARGET = "services/gateway/app/ui_routes.py"
OTHER = "services/gateway/app/static/image.html"


def _read(path: str, *, ok: bool = True) -> dict:
    result = {"path": path, "content": "implementation"}
    if not ok:
        result = {"path": path, "error": "read failed"}
    return {
        "type": "tool_finished",
        "name": "coding_read_file_lines",
        "result": result,
    }


def _plan(*, ok: bool = True) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_update_plan",
        "result": {"ok": ok, "plan": {"revision": 2}},
    }


def _edit_state() -> dict:
    return {
        "action_kind": "edit",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
        "hypothesis_causal_evidence_linked": True,
    }


def test_normal_read_then_hypothesis_remains_edit_authorized():
    state = freshness.refine_state(
        {"agent_events": [_read(TARGET), _plan()]},
        _edit_state(),
    )

    assert state["action_kind"] == "edit"
    assert "coding_apply_patch" in state["allowed_tools"]
    assert "hypothesis_evidence_postdates_plan" not in state


def test_corrective_read_after_hypothesis_requires_fresh_plan_revision():
    state = freshness.refine_state(
        {"agent_events": [_plan(), _read(TARGET)]},
        _edit_state(),
    )

    assert state["action_kind"] == "evidence"
    assert state["allowed_tools"] == ["coding_finish", "coding_update_plan"]
    assert state["hypothesis_evidence_postdates_plan"] is True
    assert "Revise the remediation hypothesis" in state["required_action"]
    assert "coding_apply_patch" not in state["allowed_tools"]


def test_second_plan_update_after_corrective_read_reenables_edit():
    state = freshness.refine_state(
        {"agent_events": [_plan(), _read(TARGET), _plan()]},
        _edit_state(),
    )

    assert state["action_kind"] == "edit"
    assert "coding_apply_patch" in state["allowed_tools"]


def test_read_of_unlinked_target_after_plan_does_not_stale_hypothesis():
    state = freshness.refine_state(
        {"agent_events": [_plan(), _read(OTHER)]},
        _edit_state(),
    )

    assert state["action_kind"] == "edit"
    assert "coding_apply_patch" in state["allowed_tools"]


def test_failed_linked_read_does_not_stale_hypothesis():
    state = freshness.refine_state(
        {"agent_events": [_plan(), _read(TARGET, ok=False)]},
        _edit_state(),
    )

    assert state["action_kind"] == "edit"


def test_failed_plan_update_after_new_evidence_does_not_refresh_hypothesis():
    state = freshness.refine_state(
        {"agent_events": [_plan(), _read(TARGET), _plan(ok=False)]},
        _edit_state(),
    )

    assert state["action_kind"] == "evidence"
    assert state["hypothesis_evidence_postdates_plan"] is True


def test_linked_read_without_recorded_plan_update_is_conservatively_blocked():
    state = freshness.refine_state(
        {"agent_events": [_read(TARGET)]},
        _edit_state(),
    )

    assert state["action_kind"] == "evidence"
    assert state["latest_hypothesis_plan_event_index"] == -1


def test_non_edit_policy_is_unchanged():
    original = {
        "action_kind": "evidence",
        "allowed_tools": ["coding_finish", "coding_update_plan"],
        "hypothesis_causal_targets": [TARGET],
    }

    state = freshness.refine_state(
        {"agent_events": [_plan(), _read(TARGET)]},
        original,
    )

    assert state == original


def test_freshness_prompt_requires_revision_without_more_inspection():
    class Base:
        _HYPOTHESIS_FIELDS = (
            "Root cause",
            "Repository evidence",
            "Competing explanation checked",
            "Expected result",
        )

    captured = {}

    class Policy:
        _provenance_prompt_context = staticmethod(lambda base, state: "original")

    freshness.install(Policy)
    prompt = Policy._provenance_prompt_context(
        Base,
        {
            "hypothesis_evidence_postdates_plan": True,
            "causal_evidence_targets": [TARGET],
            "allowed_tools": ["coding_finish", "coding_update_plan"],
        },
    )

    assert "current hypothesis is stale" in prompt
    assert TARGET in prompt
    assert "Do not edit or inspect further" in prompt
    assert "coding_update_plan" in prompt
