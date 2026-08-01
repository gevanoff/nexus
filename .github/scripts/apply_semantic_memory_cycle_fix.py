from __future__ import annotations

from pathlib import Path


AGENT_PATH = Path("services/gateway/app/coding_agent.py")
TEST_PATH = Path("services/gateway/tests/test_coding_semantic_memory.py")
WORKFLOW_PATH = Path(".github/workflows/apply-semantic-memory-cycle-fix.yml")
SCRIPT_PATH = Path(".github/scripts/apply_semantic_memory_cycle_fix.py")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_agent() -> None:
    text = AGENT_PATH.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from app import coding_model_policy\nfrom app import coding_workspace as cw\n",
        "from app import coding_model_policy\nfrom app import coding_semantic_memory\nfrom app import coding_workspace as cw\n",
        label="coding_agent import",
    )

    helper_anchor = """async def request_stop(task_id: str) -> Dict[str, Any]:
    return await request_pause(task_id)


async def _run_agent(
"""
    helper_replacement = """async def request_stop(task_id: str) -> Dict[str, Any]:
    return await request_pause(task_id)


async def _enforce_cycle_progress_decision(
    task_id: str,
    *,
    cycle: int,
    decision: ProgressDecision,
) -> None:
    checkpoint_injected = False
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
        await asyncio.to_thread(
            _append_event,
            task_id,
            {
                "type": "no_progress_recovery",
                "cycle": cycle,
                "stagnant_cycles": decision.state.stagnant_cycles,
                "summary": (
                    "A durable investigation checkpoint was injected at the no-progress boundary. "
                    "Granting one recovery cycle so the model can act on the required next action."
                ),
            },
        )
        return

    if not decision.pause:
        return

    await asyncio.to_thread(
        _append_event,
        task_id,
        {
            "type": "no_progress_limit",
            "cycle": cycle,
            "reason_code": decision.reason_code,
            "summary": decision.summary,
            "stagnant_cycles": decision.state.stagnant_cycles,
        },
    )
    raise _CodingAgentPaused(
        decision.summary,
        reason_code=decision.reason_code,
        details={
            "cycle": cycle,
            "stagnant_cycles": decision.state.stagnant_cycles,
        },
    )


async def _run_agent(
"""
    text = replace_once(
        text,
        helper_anchor,
        helper_replacement,
        label="coding_agent cycle helper",
    )

    old_pause = """            if decision.pause:
                await asyncio.to_thread(
                    _append_event,
                    task_id,
                    {
                        "type": "no_progress_limit",
                        "cycle": cycle,
                        "reason_code": decision.reason_code,
                        "summary": decision.summary,
                        "stagnant_cycles": decision.state.stagnant_cycles,
                    },
                )
                raise _CodingAgentPaused(
                    decision.summary,
                    reason_code=decision.reason_code,
                    details={
                        "cycle": cycle,
                        "stagnant_cycles": decision.state.stagnant_cycles,
                    },
                )
"""
    new_pause = """            await _enforce_cycle_progress_decision(
                task_id,
                cycle=cycle,
                decision=decision,
            )
"""
    text = replace_once(
        text,
        old_pause,
        new_pause,
        label="coding_agent no-progress enforcement",
    )
    AGENT_PATH.write_text(text, encoding="utf-8")


def patch_tests() -> None:
    text = TEST_PATH.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "import asyncio\n\nimport pytest\n",
        "import asyncio\nfrom types import SimpleNamespace\n\nimport pytest\n",
        label="semantic-memory test imports",
    )

    anchor = """def test_inactive_workspace_is_ignored(monkeypatch):
    task = _task()
    task["agent_status"] = "paused"
    messages = _install_workspace_stubs(monkeypatch, task)

    assert memory.process_task(task["id"]) is False
    assert messages == []


@pytest.mark.asyncio
async def test_runtime_start_and_stop_are_idempotent(monkeypatch):
"""
    replacement = """def test_inactive_workspace_is_ignored(monkeypatch):
    task = _task()
    task["agent_status"] = "paused"
    messages = _install_workspace_stubs(monkeypatch, task)

    assert memory.process_task(task["id"]) is False
    assert messages == []


@pytest.mark.asyncio
async def test_cycle_boundary_checkpoint_recovers_before_terminal_pause(monkeypatch):
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
        lambda task_id: calls.append(("checkpoint", task_id)) or True,
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
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    with pytest.raises(coding_agent._CodingAgentPaused) as exc_info:
        await coding_agent._enforce_cycle_progress_decision(
            "code_abcdef123456",
            cycle=8,
            decision=decision,
        )

    assert exc_info.value.reason_code == "no_progress_limit"
    assert calls == [
        ("checkpoint", "code_abcdef123456"),
        ("event", "no_progress_limit"),
    ]


@pytest.mark.asyncio
async def test_cycle_boundary_records_checkpoint_failure_before_pausing(monkeypatch):
    calls = []
    decision = SimpleNamespace(
        pause=True,
        reason_code="no_progress_limit",
        summary="paused",
        state=SimpleNamespace(stagnant_cycles=8),
    )

    def fail_checkpoint(_task_id):
        calls.append(("checkpoint", "failed"))
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(coding_agent.coding_semantic_memory, "process_task", fail_checkpoint)
    monkeypatch.setattr(
        coding_agent,
        "_append_event",
        lambda task_id, event: calls.append(("event", event["type"])) or event,
    )

    with pytest.raises(coding_agent._CodingAgentPaused):
        await coding_agent._enforce_cycle_progress_decision(
            "code_abcdef123456",
            cycle=8,
            decision=decision,
        )

    assert calls == [
        ("checkpoint", "failed"),
        ("event", "investigation_checkpoint_error"),
        ("event", "no_progress_limit"),
    ]


@pytest.mark.asyncio
async def test_runtime_start_and_stop_are_idempotent(monkeypatch):
"""
    text = replace_once(
        text,
        anchor,
        replacement,
        label="semantic-memory regression insertion",
    )
    TEST_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    patch_agent()
    patch_tests()
    WORKFLOW_PATH.unlink()
    SCRIPT_PATH.unlink()


if __name__ == "__main__":
    main()
