from __future__ import annotations

from app import coding_forced_action as forced


def test_targeted_evidence_accepts_read_payload_without_ok() -> None:
    assert forced._targeted_evidence_result_succeeded(
        {
            "path": "services/gateway/app/example.py",
            "start_line": 10,
            "line_count": 20,
            "content": "def example():\n    return True\n",
        }
    ) is True


def test_targeted_evidence_requires_literal_true_when_ok_is_present() -> None:
    assert forced._targeted_evidence_result_succeeded({"ok": True, "matches": []}) is True
    assert forced._targeted_evidence_result_succeeded({"ok": False, "matches": []}) is False
    assert forced._targeted_evidence_result_succeeded({"ok": "true", "matches": []}) is False


def test_targeted_evidence_rejects_errors_and_empty_payloads() -> None:
    assert forced._targeted_evidence_result_succeeded({"error": "forced_action_tool_rejected"}) is False
    assert forced._targeted_evidence_result_succeeded({"error": "read failed"}) is False
    assert forced._targeted_evidence_result_succeeded({}) is False
