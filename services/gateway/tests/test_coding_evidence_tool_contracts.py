from __future__ import annotations

from app import coding_forced_action as forced


def test_targeted_evidence_accepts_read_payload_without_ok() -> None:
    assert forced._targeted_evidence_result_succeeded(
        "coding_read_file_lines",
        {
            "path": "services/gateway/app/example.py",
            "start_line": 10,
            "line_count": 20,
            "content": "def example():\n    return True\n",
        },
    ) is True


def test_search_evidence_requires_literal_true() -> None:
    assert forced._targeted_evidence_result_succeeded(
        "coding_search_text", {"ok": True, "matches": []}
    ) is True
    assert forced._targeted_evidence_result_succeeded(
        "coding_search_text", {"matches": []}
    ) is False
    assert forced._targeted_evidence_result_succeeded(
        "coding_search_text", {"ok": False, "matches": []}
    ) is False
    assert forced._targeted_evidence_result_succeeded(
        "coding_search_text", {"ok": "true", "matches": []}
    ) is False


def test_targeted_evidence_rejects_errors_and_malformed_read_payloads() -> None:
    assert forced._targeted_evidence_result_succeeded(
        "coding_read_file_lines", {"error": "forced_action_tool_rejected"}
    ) is False
    assert forced._targeted_evidence_result_succeeded(
        "coding_read_file_lines", {"error": "read failed"}
    ) is False
    assert forced._targeted_evidence_result_succeeded(
        "coding_read_file_lines", {}
    ) is False
    assert forced._targeted_evidence_result_succeeded(
        "coding_read_file_lines", {"path": "example.py"}
    ) is False
