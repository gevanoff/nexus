from __future__ import annotations


def test_guarded_routes_installs_failed_edit_recovery_after_freshness():
    from app import coding_routes_guarded as guarded_routes

    policy = guarded_routes.coding_evidence_policy
    assert policy._coding_evidence_freshness_installed is True
    assert policy._coding_evidence_range_provenance_installed is True
    assert policy._coding_failed_edit_recovery_installed is True
