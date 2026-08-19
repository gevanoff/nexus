from __future__ import annotations

from app import coding_contract_hardening as hardening
from app import coding_contract_path_safety as path_safety
from app import coding_evidence_freshness as freshness


TARGET = "services/gateway/app/ui_routes.py"


# The guarded routes install this in production. Install explicitly so this
# focused test does not depend on module import order in the full suite.
path_safety.install(hardening)


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


def _plan_event(revision: int, *, ts: float) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_update_plan",
        "ts": ts,
        "result": {"ok": True, "plan": {"revision": revision}},
    }


def _read_event(*, ts: float) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_read_file_lines",
        "ts": ts,
        "result": {"path": TARGET, "content": "implementation"},
    }


def test_arbitrary_suffixless_nested_repository_path_can_open_corrective_read():
    targets = hardening._resolve_asserted_targets(
        "bin/server contains the runtime dispatch gate",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == ["bin/server"]
    assert hardening._target_is_causal("bin/server")


def test_explicit_root_suffixless_repository_file_can_open_corrective_read():
    quoted = hardening._resolve_asserted_targets(
        "`BUILD` defines the generated service target",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )
    conventional = hardening._resolve_asserted_targets(
        "BUILD defines the generated service target",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert quoted == ["BUILD"]
    assert conventional == ["BUILD"]


def test_explicit_root_suffixless_target_is_locked_to_repository_root():
    assert hardening._read_matches_target("BUILD", "BUILD")
    assert not hardening._read_matches_target("subdir/BUILD", "BUILD")


def test_explicit_root_suffixless_target_is_not_reinterpreted_by_unique_candidate():
    targets = hardening._resolve_asserted_targets(
        "`BUILD` defines the generated service target",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": ["subdir/BUILD"],
        },
    )

    assert targets == ["BUILD"]
    assert not hardening._read_matches_target("subdir/BUILD", targets[0])


def test_explicit_nested_suffixless_path_remains_exactly_path_locked():
    targets = hardening._resolve_asserted_targets(
        "subdir/BUILD defines the nested generated target",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == ["subdir/BUILD"]
    assert hardening._read_matches_target("subdir/BUILD", targets[0])
    assert not hardening._read_matches_target("BUILD", targets[0])


def test_suffixless_recovery_keeps_test_and_docs_exclusions():
    targets = hardening._resolve_asserted_targets(
        "tests/server and docs/BUILD describe acceptance behavior",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == []


def test_newer_api_or_ui_plan_revision_overrides_surviving_old_tool_event():
    state = freshness.refine_state(
        {
            "project_plan": {"revision": 3, "updated_at": 102.0},
            "agent_events": [
                _plan_event(2, ts=99.0),
                _read_event(ts=101.0),
            ],
        },
        _edit_state(),
    )

    assert state["action_kind"] == "edit"
    assert "hypothesis_evidence_postdates_plan" not in state


def test_newer_revision_that_still_predates_read_remains_stale():
    state = freshness.refine_state(
        {
            "project_plan": {"revision": 3, "updated_at": 100.0},
            "agent_events": [
                _plan_event(2, ts=99.0),
                _read_event(ts=101.0),
            ],
        },
        _edit_state(),
    )

    assert state["action_kind"] == "evidence"
    assert state["hypothesis_freshness_source"] == "current_plan_timestamp"
    assert state["latest_hypothesis_plan_event_revision"] == 2


def test_current_revision_event_keeps_event_order_authoritative():
    state = freshness.refine_state(
        {
            "project_plan": {"revision": 2, "updated_at": 99.0},
            "agent_events": [
                _plan_event(2, ts=99.0),
                _read_event(ts=101.0),
            ],
        },
        _edit_state(),
    )

    assert state["action_kind"] == "evidence"
    assert state["hypothesis_freshness_source"] == "event_order"


def test_mismatched_revision_without_timestamp_does_not_invent_staleness():
    state = freshness.refine_state(
        {
            "project_plan": {"revision": 3},
            "agent_events": [
                _plan_event(2, ts=99.0),
                _read_event(ts=101.0),
            ],
        },
        _edit_state(),
    )

    assert state["action_kind"] == "edit"
