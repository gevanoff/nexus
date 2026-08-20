from __future__ import annotations

from app import coding_evidence_policy as provenance
from app import coding_evidence_range_provenance as range_provenance
from app import coding_forced_action as forced


TARGET = "services/gateway/app/ui_routes.py"


def _state() -> dict:
    return {
        "requires_hypothesis": True,
        "action_kind": "edit",
        "canonical_action_kind": "edit",
        "canonical_required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        "activation_plan_revision": 0,
        "activation_event_count": 0,
        "activated_at": 10.0,
        "run_id": "run-1",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
        "hypothesis_causal_evidence_linked": True,
        "evidence_provenance_enforced": True,
    }


def _started() -> dict:
    return {"ts": 1.0, "type": "started", "run_id": "run-1", "backend": "local_mlx"}


def _read(*, ranged: bool = True, start: int = 1538, end: int = 1557) -> list[dict]:
    result = {"ok": True, "path": TARGET, "content": "bounded implementation"}
    if ranged:
        result.update(
            {
                "start_line": start,
                "end_line": end,
                "line_count": end - start + 1,
                "total_lines": 7000,
                "truncated": True,
            }
        )
    return [
        {
            "ts": 2.0,
            "type": "tool_started",
            "tool_call_id": "read-1",
            "name": "coding_read_file_lines",
            "args": {"path": TARGET, "start_line": start, "line_count": end - start + 1},
        },
        {
            "ts": 3.0,
            "type": "tool_finished",
            "tool_call_id": "read-1",
            "name": "coding_read_file_lines",
            "result": result,
        },
    ]


def _plan(repository_evidence: str) -> dict:
    return {
        "revision": 1,
        "goal": "Restore InvokeAI management navigation",
        "note": (
            "Root cause: backend management metadata is attached in the wrong control-flow location.\n"
            f"Repository evidence: {repository_evidence}\n"
            "Competing explanation checked: frontend rendering still consumes management.ui_url.\n"
            "Expected result: management navigation survives model-catalog failure."
        ),
        "items": [],
    }


def _base_effective(repository_evidence: str, *, ranged: bool = True) -> tuple[dict, dict]:
    # Start from the state produced by the existing path-level provenance gate.
    # Do not call the globally monkey-patched gate here: other integration tests
    # may already have installed this range overlay during collection.
    task = {
        "agent_events": [_started(), *_read(ranged=ranged)],
        "project_plan": _plan(repository_evidence),
    }
    return task, _state()


def test_modern_bounded_read_requires_line_range_in_repository_evidence():
    task, state = _base_effective(TARGET)

    refined = range_provenance.refine_state(provenance, forced, task, state)

    assert refined["action_kind"] == "evidence"
    assert refined["allowed_tools"] == ["coding_finish", "coding_update_plan"]
    assert refined["hypothesis_evidence_range_required"] is True
    assert refined["hypothesis_causal_evidence_linked"] is False
    assert refined["hypothesis_causal_targets"] == []
    assert refined["causal_evidence_ranges"] == [
        {"path": TARGET, "start_line": 1538, "end_line": 1557}
    ]
    assert "path/to/file.py:120-145" in refined["required_action"]


def test_verified_colon_range_reauthorizes_edit():
    task, state = _base_effective(f"{TARGET}:1538-1557 shows the relevant control flow")

    refined = range_provenance.refine_state(provenance, forced, task, state)

    assert refined["action_kind"] == "edit"
    assert refined["hypothesis_causal_evidence_linked"] is True
    assert refined["hypothesis_causal_targets"] == [TARGET]
    assert refined["hypothesis_causal_evidence_ranges"] == [
        {"path": TARGET, "start_line": 1538, "end_line": 1557}
    ]


def test_github_style_single_line_inside_read_is_accepted():
    task, state = _base_effective(f"{TARGET}#L1544 demonstrates the condition")

    refined = range_provenance.refine_state(provenance, forced, task, state)

    assert refined["action_kind"] == "edit"
    assert refined["hypothesis_causal_evidence_ranges"] == [
        {"path": TARGET, "start_line": 1544, "end_line": 1544}
    ]


def test_cited_range_outside_verified_read_does_not_unlock_edit():
    task, state = _base_effective(f"{TARGET}:1600-1650 contains the failing branch")

    refined = range_provenance.refine_state(provenance, forced, task, state)

    assert refined["action_kind"] == "evidence"
    assert refined["hypothesis_evidence_range_required"] is True


def test_range_that_extends_beyond_verified_read_is_rejected():
    task, state = _base_effective(f"{TARGET}:1540-1600 contains the failing branch")

    refined = range_provenance.refine_state(provenance, forced, task, state)

    assert refined["action_kind"] == "evidence"
    assert refined["hypothesis_evidence_range_required"] is True


def test_legacy_read_without_range_metadata_preserves_path_only_compatibility():
    task, state = _base_effective(TARGET, ranged=False)

    refined = range_provenance.refine_state(provenance, forced, task, state)

    assert refined["action_kind"] == "edit"
    assert refined["hypothesis_causal_evidence_linked"] is True
    assert "hypothesis_evidence_range_required" not in refined


def test_range_prompt_lists_only_verified_span_and_requires_plan_update():
    class Policy:
        apply_provenance_gate = staticmethod(lambda forced_action, task, state: dict(state))
        _provenance_prompt_context = staticmethod(lambda base, state: "original")

    range_provenance.install(Policy)
    prompt = Policy._provenance_prompt_context(
        object(),
        {
            "hypothesis_evidence_range_required": True,
            "causal_evidence_ranges": [
                {"path": TARGET, "start_line": 1538, "end_line": 1557}
            ],
            "allowed_tools": ["coding_finish", "coding_update_plan"],
        },
    )

    assert "file path alone is not sufficient" in prompt
    assert f"{TARGET}:1538-1557" in prompt
    assert "Do not inspect further" in prompt
    assert "coding_update_plan" in prompt
