from __future__ import annotations


def test_guarded_routes_installs_edit_evidence_continuity():
    from app import coding_routes_guarded as guarded_routes

    dispatch = guarded_routes.coding_execution_dispatch
    debug_report = guarded_routes.coding_debug_report
    persistence = guarded_routes.coding_hypothesis_persistence

    assert dispatch._coding_edit_evidence_continuity_installed is True
    assert dispatch._coding_evidence_replay_observability_installed is True
    assert debug_report._coding_effective_policy_debug_installed is True
    assert hasattr(persistence, "_verified_evidence_digest_before_edit_continuity")
