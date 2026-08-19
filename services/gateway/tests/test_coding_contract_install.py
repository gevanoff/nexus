from __future__ import annotations

from types import SimpleNamespace

from app import coding_contract_hardening as hardening
from app import coding_forced_action as forced
from app import coding_routes_guarded  # noqa: F401 - imports install the production overlays


def _period_plan() -> dict:
    return {
        "revision": 1,
        "goal": "Fix the Image UI",
        "note": (
            "Root cause: catalog failure suppresses management metadata. "
            "Repository evidence: services/gateway/app/ui_routes.py returns before UI metadata is attached. "
            "Competing explanation checked: the HTML target and JavaScript renderer are present. "
            "Expected result: the InvokeAI management link remains available when catalog discovery fails."
        ),
        "items": [],
    }


def test_production_forced_action_parser_accepts_sentence_separated_contract_after_install():
    ready, fields = forced._structured_hypothesis(
        {"project_plan": _period_plan()},
        {"activation_plan_revision": 0},
    )

    assert ready is True
    assert set(fields) == set(forced._HYPOTHESIS_FIELDS)
    assert "services/gateway/app/ui_routes.py" in fields["Repository evidence"]


def test_text_tool_transport_diagnostic_uses_execution_policy_authorized_tools():
    request = SimpleNamespace(
        x_nexus={
            "coding_execution_policy": {
                "action_kind": "evidence",
                "allowed_tools": [
                    "coding_finish",
                    "coding_read_file_lines",
                    "coding_update_plan",
                ],
            }
        }
    )
    diagnostics = (
        {
            "name": "coding_apply_patch",
            "reason": "malformed tool name",
            "allowed_tool_names": [],
        },
    )

    enriched = hardening._diagnostics_with_policy_tools(diagnostics, request)

    assert enriched[0]["allowed_tool_names"] == [
        "coding_finish",
        "coding_read_file_lines",
        "coding_update_plan",
    ]
    notice = hardening.diagnostic_notice(enriched[0])
    assert "coding_apply_patch" in notice
    assert "coding_read_file_lines" in notice
    assert "coding_update_plan" in notice


def test_missing_execution_policy_does_not_invent_authorized_tools():
    diagnostics = (
        {
            "name": "bad name",
            "reason": "malformed tool name",
            "allowed_tool_names": [],
        },
    )

    enriched = hardening._diagnostics_with_policy_tools(
        diagnostics,
        SimpleNamespace(x_nexus={}),
    )

    assert enriched == diagnostics
    assert "not available in transport diagnostic" in hardening.diagnostic_notice(enriched[0])
