from __future__ import annotations

from pathlib import Path


AGENT = Path("services/gateway/app/coding_agent.py")
TESTS = Path("services/gateway/tests/test_coding_semantic_memory.py")
SELF = Path(".github/scripts/apply_concurrent_checkpoint_fix.py")
WORKFLOW = Path(".github/workflows/apply-concurrent-checkpoint-fix.yml")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_agent() -> None:
    text = AGENT.read_text(encoding="utf-8")
    old = '''    checkpoint_injected = False
    try:
        checkpoint_injected = bool(
            await asyncio.to_thread(coding_semantic_memory.process_task, task_id)
        )
    except Exception as exc:
        logger.warning(
            "coding semantic checkpoint failed task_id=%s cycle=%s (%s: %s)",
            task_id,
            cycle,
            type(exc).__name__,
            exc,
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "investigation_checkpoint_error",
                "cycle": cycle,
                "summary": f"{type(exc).__name__}: {exc}",
            },
        )

    if checkpoint_injected and decision.pause:
'''
    new = '''    checkpoint_injected = False
    checkpoint_check_completed = False
    try:
        checkpoint_injected = bool(
            await asyncio.to_thread(coding_semantic_memory.process_task, task_id)
        )
        checkpoint_check_completed = True
    except Exception as exc:
        logger.warning(
            "coding semantic checkpoint failed task_id=%s cycle=%s (%s: %s)",
            task_id,
            cycle,
            type(exc).__name__,
            exc,
        )
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "investigation_checkpoint_error",
                "cycle": cycle,
                "summary": f"{type(exc).__name__}: {exc}",
            },
        )

    fresh_checkpoint = checkpoint_injected
    if decision.pause and not fresh_checkpoint and checkpoint_check_completed:
        try:
            latest = await asyncio.to_thread(cw.load_task, task_id)
            checkpoint = (
                latest.get("agent_investigation_checkpoint")
                if isinstance(latest.get("agent_investigation_checkpoint"), dict)
                else {}
            )
            fresh_checkpoint = (
                int(checkpoint.get("cycle") or 0) == cycle
                and str(checkpoint.get("run_id") or "")
                == str(latest.get("agent_run_id") or "")
            )
        except Exception as exc:
            logger.warning(
                "coding semantic checkpoint freshness check failed task_id=%s cycle=%s (%s: %s)",
                task_id,
                cycle,
                type(exc).__name__,
                exc,
            )

    if fresh_checkpoint and decision.pause:
'''
    text = replace_once(text, old, new, "controller checkpoint race")
    AGENT.write_text(text, encoding="utf-8")


def patch_tests() -> None:
    text = TESTS.read_text(encoding="utf-8")
    anchor = '''@pytest.mark.asyncio
async def test_cycle_boundary_pauses_after_checkpoint_credit_is_used(monkeypatch):
'''
    concurrent_test = '''@pytest.mark.asyncio
async def test_cycle_boundary_recovers_when_background_scanner_won_claim(monkeypatch):
    calls = []
    decision = SimpleNamespace(
        pause=True,
        reason_code="no_progress_limit",
        summary="paused",
        state=SimpleNamespace(stagnant_cycles=8),
    )
    monkeypatch.setattr(
        coding_agent.coding_semantic_memory,
        "process_task",
        lambda task_id: calls.append(("checkpoint", task_id)) or False,
    )
    monkeypatch.setattr(
        coding_agent.cw,
        "load_task",
        lambda _task_id: {
            "agent_run_id": "run-2",
            "agent_investigation_checkpoint": {"run_id": "run-2", "cycle": 8},
        },
    )
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    await coding_agent._enforce_cycle_progress_decision(
        "code_abcdef123456",
        cycle=8,
        decision=decision,
    )

    assert calls == [
        ("checkpoint", "code_abcdef123456"),
        ("event", "no_progress_recovery"),
    ]


@pytest.mark.asyncio
async def test_cycle_boundary_pauses_after_checkpoint_credit_is_used(monkeypatch):
'''
    text = replace_once(text, anchor, concurrent_test, "concurrent checkpoint test insertion")

    old = '''    monkeypatch.setattr(
        coding_agent.coding_semantic_memory,
        "process_task",
        lambda task_id: calls.append(("checkpoint", task_id)) or False,
    )
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
'''
    new = '''    monkeypatch.setattr(
        coding_agent.coding_semantic_memory,
        "process_task",
        lambda task_id: calls.append(("checkpoint", task_id)) or False,
    )
    monkeypatch.setattr(
        coding_agent.cw,
        "load_task",
        lambda _task_id: {
            "agent_run_id": "run-2",
            "agent_investigation_checkpoint": {"run_id": "run-2", "cycle": 7},
        },
    )
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
'''
    # The first matching false-result block now belongs to the newly inserted
    # concurrent test. Replace the second occurrence for the exhausted-credit case.
    first = text.find(old)
    second = text.find(old, first + len(old)) if first >= 0 else -1
    if second < 0:
        raise SystemExit("exhausted-credit monkeypatch anchor not found")
    text = text[:second] + text[second:].replace(old, new, 1)

    TESTS.write_text(text, encoding="utf-8")


def main() -> None:
    patch_agent()
    patch_tests()
    SELF.unlink()
    WORKFLOW.unlink()


if __name__ == "__main__":
    main()
