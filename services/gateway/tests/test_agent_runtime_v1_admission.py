from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import agent_runtime_v1 as ar


def test_request_is_heavy_for_unbounded_tier_one():
    assert ar._request_is_heavy(tier=1, tools_allowlist=None) is True


def test_request_is_light_for_coding_supervision_allowlist():
    assert ar._request_is_heavy(
        tier=1,
        tools_allowlist=["current_time", "tool_manifest", "coding_task_monitor", "coding_task_inspect", "coding_task_intervene"],
    ) is False


def test_request_is_light_for_selected_coding_workspace_create_tool():
    assert ar._request_is_heavy(tier=1, tools_allowlist=["tool_manifest", "coding_task_create"]) is False


def test_request_is_light_for_selected_model_integration_tool():
    assert ar._request_is_heavy(tier=1, tools_allowlist=["tool_manifest", "coding_model_integration"]) is False


def test_request_is_heavy_for_shell_tier():
    assert ar._request_is_heavy(tier=2, tools_allowlist=["shell"]) is True


def test_request_is_heavy_for_general_write_tool():
    assert ar._request_is_heavy(tier=1, tools_allowlist=["write_file"]) is True
