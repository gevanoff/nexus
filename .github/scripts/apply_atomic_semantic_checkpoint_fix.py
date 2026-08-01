from __future__ import annotations

from pathlib import Path


AGENT = Path("services/gateway/app/coding_agent.py")
MEMORY = Path("services/gateway/app/coding_semantic_memory.py")
TESTS = Path("services/gateway/tests/test_coding_semantic_memory.py")
SELF = Path(".github/scripts/apply_atomic_semantic_checkpoint_fix.py")
WORKFLOW = Path(".github/workflows/apply-atomic-semantic-checkpoint-fix.yml")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_memory() -> None:
    text = MEMORY.read_text(encoding="utf-8")
    old = '''def _claim_checkpoint(task_id: str, checkpoint: Dict[str, Any]) -> bool:
    claimed = {"value": False}
    state_key = str(checkpoint.get("state_key") or "")
    run_id = str(checkpoint.get("run_id") or "")

    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
            return
        if str(task.get("agent_run_id") or "") != run_id:
            return
        if str(task.get("agent_investigation_guidance_state_key") or "") == state_key:
            return
        task["agent_investigation_checkpoint"] = dict(checkpoint)
        task["agent_investigation_guidance_state_key"] = state_key
        events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
        events.append(
            {
                "type": "investigation_checkpoint",
                "ts": int(time.time()),
                "cycle": checkpoint.get("cycle"),
                "stagnant_cycles": checkpoint.get("stagnant_cycles"),
                "state_key": state_key,
                "summary": "Persisted a bounded investigation checkpoint before further broad inspection.",
            }
        )
        task["agent_events"] = events[-1000:]
        claimed["value"] = True

    cw.mutate_task(task_id, apply)
    return claimed["value"]


def _release_checkpoint_claim(task_id: str, state_key: str, error: str) -> None:
    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_investigation_guidance_state_key") or "") == state_key:
            task["agent_investigation_guidance_state_key"] = ""
            task["agent_investigation_checkpoint_error"] = _clip(error, 1000)

    cw.mutate_task(task_id, apply)


def process_task(task_id: str) -> bool:
    task = cw.load_task(task_id)
    if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
        return False
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    stagnant = _as_int(progress.get("stagnant_cycles"))
    if stagnant < _stagnation_threshold(task):
        return False
    checkpoint = build_investigation_checkpoint(task)
    if not _claim_checkpoint(task_id, checkpoint):
        return False
    state_key = str(checkpoint.get("state_key") or "")
    try:
        cw.append_guidance_message(
            task_id,
            message=render_checkpoint_guidance(checkpoint),
            actor="nexus-controller",
        )
    except Exception as exc:
        _release_checkpoint_claim(task_id, state_key, f"{type(exc).__name__}: {exc}")
        raise
    return True
'''
    new = '''def _claim_checkpoint(task_id: str, checkpoint: Dict[str, Any]) -> bool:
    claimed = {"value": False}
    state_key = str(checkpoint.get("state_key") or "")
    run_id = str(checkpoint.get("run_id") or "")
    guidance = render_checkpoint_guidance(checkpoint)

    def apply(task: Dict[str, Any]) -> None:
        if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
            return
        if str(task.get("agent_run_id") or "") != run_id:
            return
        if str(task.get("agent_investigation_guidance_state_key") or "") == state_key:
            return
        now = time.time()
        task["agent_investigation_checkpoint"] = dict(checkpoint)
        task["agent_investigation_guidance_state_key"] = state_key
        events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
        events.append(
            {
                "type": "investigation_checkpoint",
                "ts": int(now),
                "cycle": checkpoint.get("cycle"),
                "stagnant_cycles": checkpoint.get("stagnant_cycles"),
                "state_key": state_key,
                "summary": "Persisted a bounded investigation checkpoint before further broad inspection.",
            }
        )
        task["agent_events"] = events[-1000:]
        messages = task.get("guidance_messages") if isinstance(task.get("guidance_messages"), list) else []
        messages.append(
            {
                "ts": now,
                "role": "user",
                "actor": "nexus-controller",
                "run_id": run_id,
                "content": guidance,
            }
        )
        task["guidance_messages"] = messages[-200:]
        task["last_guidance_at"] = now
        task.pop("agent_investigation_checkpoint_error", None)
        claimed["value"] = True

    cw.mutate_task(task_id, apply)
    return claimed["value"]


def process_task(task_id: str) -> bool:
    task = cw.load_task(task_id)
    if str(task.get("agent_status") or "").strip().lower() not in _ACTIVE_STATUSES:
        return False
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    stagnant = _as_int(progress.get("stagnant_cycles"))
    if stagnant < _stagnation_threshold(task):
        return False
    return _claim_checkpoint(task_id, build_investigation_checkpoint(task))
'''
    text = replace_once(text, old, new, "atomic checkpoint transaction")
    MEMORY.write_text(text, encoding="utf-8")


def patch_agent() -> None:
    text = AGENT.read_text(encoding="utf-8")
    old_start = '''            cycle += 1
            _raise_if_paused(task_id)
            new_guidance, seen_guidance_count = await asyncio.to_thread(_new_guidance_since, task_id, seen_guidance_count)
'''
    new_start = '''            cycle += 1
            _raise_if_paused(task_id)
            await asyncio.to_thread(_mutate_task, task_id, {"agent_cycle": cycle, "agent_last_event_at": now_unix()})
            await asyncio.to_thread(_update_run_record, task_id, run_id, {"cycle": cycle})
            await asyncio.to_thread(_append_event, task_id, {"type": "cycle_started", "cycle": cycle})
            new_guidance, seen_guidance_count = await asyncio.to_thread(_new_guidance_since, task_id, seen_guidance_count)
'''
    text = replace_once(text, old_start, new_start, "publish cycle before guidance poll")
    old_late = '''            await asyncio.to_thread(_mutate_task, task_id, {"agent_cycle": cycle, "agent_last_event_at": now_unix()})
            await asyncio.to_thread(_update_run_record, task_id, run_id, {"cycle": cycle})
            await asyncio.to_thread(_append_event, task_id, {"type": "cycle_started", "cycle": cycle})

            request_text_tool_mode = not _backend_supports_tool_calling(backend)
'''
    new_late = '''            request_text_tool_mode = not _backend_supports_tool_calling(backend)
'''
    text = replace_once(text, old_late, new_late, "remove late cycle publication")
    AGENT.write_text(text, encoding="utf-8")


def patch_tests() -> None:
    text = TESTS.read_text(encoding="utf-8")
    old = '''def _install_workspace_stubs(monkeypatch, task):
    messages = []

    def mutate(_task_id, mutator):
        mutator(task)
        return task

    def append_guidance(_task_id, *, message, actor):
        messages.append({"message": message, "actor": actor})
        task.setdefault("guidance_messages", []).append(
            {"content": message, "actor": actor, "ts": 123.0}
        )
        task["last_guidance_at"] = 123.0
        return task

    monkeypatch.setattr(memory.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(memory.cw, "mutate_task", mutate)
    monkeypatch.setattr(memory.cw, "append_guidance_message", append_guidance)
    monkeypatch.setattr(memory.cw, "normalize_coding_mission", lambda value: value["mission"])
    monkeypatch.setattr(memory.cw, "normalize_project_plan", lambda value, fallback_goal="": value)
    return messages
'''
    new = '''def _install_workspace_stubs(monkeypatch, task):
    messages = []

    def mutate(_task_id, mutator):
        before = len(task.get("guidance_messages") or [])
        mutator(task)
        for item in (task.get("guidance_messages") or [])[before:]:
            messages.append(
                {
                    "message": str(item.get("content") or ""),
                    "actor": str(item.get("actor") or ""),
                }
            )
        return task

    monkeypatch.setattr(memory.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(memory.cw, "mutate_task", mutate)
    monkeypatch.setattr(memory.cw, "normalize_coding_mission", lambda value: value["mission"])
    monkeypatch.setattr(memory.cw, "normalize_project_plan", lambda value, fallback_goal="": value)
    return messages
'''
    text = replace_once(text, old, new, "atomic guidance test stub")

    anchor = '''def test_stagnation_checkpoint_is_persisted_and_injected_once(monkeypatch):
    task = _task()
    messages = _install_workspace_stubs(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
'''
    replacement = '''def test_stagnation_checkpoint_is_persisted_and_injected_once(monkeypatch):
    task = _task()
    messages = _install_workspace_stubs(monkeypatch, task)
    monkeypatch.setattr(
        memory.cw,
        "append_guidance_message",
        lambda *args, **kwargs: pytest.fail("checkpoint guidance must use the atomic task mutation"),
    )

    assert memory.process_task(task["id"]) is True
'''
    text = replace_once(text, anchor, replacement, "atomic guidance regression")
    TESTS.write_text(text, encoding="utf-8")


def main() -> None:
    patch_memory()
    patch_agent()
    patch_tests()
    SELF.unlink()
    WORKFLOW.unlink()


if __name__ == "__main__":
    main()
