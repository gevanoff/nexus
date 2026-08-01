from __future__ import annotations

from pathlib import Path


PATH = Path("services/gateway/tests/test_coding_runtime_guardrails.py")
SELF = Path(".github/scripts/update_semantic_memory_guardrail_test.py")


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    old_name = "async def test_real_agent_loop_evaluates_once_per_multi_tool_cycle(monkeypatch) -> None:"
    new_name = "async def test_real_agent_loop_grants_one_semantic_recovery_then_pauses(monkeypatch) -> None:"
    if text.count(old_name) != 1:
        raise SystemExit("real-loop test name anchor not found exactly once")
    text = text.replace(old_name, new_name, 1)

    old_assertions = """    assert tool_calls == 8 * len(batch)
    assert task["agent_status"] == "paused"
    assert task["agent_stop_reason_code"] == "no_progress_limit"
    assert task["agent_progress_state"]["stagnant_cycles"] == 8
    assert task["agent_pause_requested"] is False
"""
    new_assertions = """    # The cycle-four semantic checkpoint becomes controller guidance on the
    # following cycle and earns exactly one reset. With no edit, validation,
    # review, plan-state change, or finish transition afterward, the run still
    # terminates deterministically after a second stagnant streak.
    assert tool_calls == 13 * len(batch)
    event_types = [str(item.get("type") or "") for item in task["agent_events"]]
    assert event_types.count("investigation_checkpoint") == 1
    assert event_types.count("no_progress_limit") == 1
    assert task["agent_status"] == "paused"
    assert task["agent_stop_reason_code"] == "no_progress_limit"
    assert task["agent_progress_state"]["stagnant_cycles"] == 8
    assert task["agent_pause_requested"] is False
"""
    if text.count(old_assertions) != 1:
        raise SystemExit("real-loop assertion anchor not found exactly once")
    text = text.replace(old_assertions, new_assertions, 1)
    PATH.write_text(text, encoding="utf-8")
    SELF.unlink()


if __name__ == "__main__":
    main()
