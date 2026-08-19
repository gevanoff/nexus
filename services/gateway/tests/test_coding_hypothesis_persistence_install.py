from __future__ import annotations


def test_guarded_routes_installs_hypothesis_persistence_overlay():
    from app import coding_routes_guarded as guarded_routes

    agent = guarded_routes.guarded_agent._agent
    assert agent._coding_hypothesis_persistence_installed is True
    assert agent._coding_plan_edit_serialization_installed is True
    assert guarded_routes.coding_execution_dispatch._coding_verified_evidence_handoff_installed is True
    assert guarded_routes.cw._coding_project_plan_note_marker_installed is True
