from __future__ import annotations

from app import coding_policy_rejection_recovery as recovery


READ = "coding_read_file_lines"
EDIT = "coding_apply_patch"
FINISH = "coding_finish"


def _diagnostic(name: str, *, reason: str = "unknown tool name") -> dict:
    return {
        "reason": reason,
        "name": name,
        "allowed_tool_names": [EDIT, FINISH],
    }


def test_known_policy_rejection_after_five_noise_diagnostics_is_preserved():
    raw = [
        _diagnostic(f"coding_hallucinated_{index}")
        for index in range(5)
    ]
    raw.append(_diagnostic(READ))

    safe = recovery._safe_diagnostics(raw)
    response = {
        recovery._TRUSTED_DIAGNOSTICS_KEY: [dict(item) for item in safe]
    }
    recovered = recovery._recoverable_policy_diagnostics(
        response,
        known_tools={READ, EDIT, FINISH},
    )

    assert len(safe) == 6
    assert [item["name"] for item in recovered] == [READ]


def test_safe_diagnostics_preserves_complete_authorized_tool_list():
    allowed = [f"coding_tool_{index}" for index in range(25)]
    raw = [
        {
            "reason": "unknown tool name",
            "name": READ,
            "allowed_tool_names": allowed,
        }
    ]

    safe = recovery._safe_diagnostics(raw)

    assert list(safe[0]["allowed_tool_names"]) == allowed
