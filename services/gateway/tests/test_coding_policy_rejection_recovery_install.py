from __future__ import annotations


def test_guarded_routes_installs_policy_rejection_recovery():
    from app import coding_routes_guarded as guarded_routes

    agent = guarded_routes.guarded_agent._agent
    assert agent._coding_policy_rejection_recovery_installed is True
    assert (
        guarded_routes.coding_stagnation_resilience._coding_inspection_ledger_integrity_installed
        is True
    )
