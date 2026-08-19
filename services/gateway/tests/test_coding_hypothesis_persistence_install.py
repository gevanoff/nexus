from __future__ import annotations


def test_guarded_routes_installs_hypothesis_persistence_overlay():
    from app import coding_routes_guarded as guarded_routes

    agent = guarded_routes.guarded_agent._agent
    assert agent._coding_hypothesis_persistence_installed is True
