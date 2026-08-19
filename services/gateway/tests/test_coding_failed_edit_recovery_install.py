from __future__ import annotations


def test_guarded_routes_installs_failed_edit_recovery_after_freshness():
    from app import coding_routes_guarded as guarded_routes

    assert guarded_routes.coding_evidence_policy._coding_evidence_freshness_installed is True
    assert guarded_routes.coding_evidence_policy._coding_failed_edit_recovery_installed is True
