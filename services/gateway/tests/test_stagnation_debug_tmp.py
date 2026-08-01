from __future__ import annotations

import json

import pytest

from services.gateway.tests import test_coding_runtime_guardrails as existing


@pytest.mark.asyncio
async def test_trace_real_loop_stagnation_state(monkeypatch):
    try:
        await existing.test_real_agent_loop_grants_one_semantic_recovery_then_pauses(monkeypatch)
    except AssertionError as exc:
        cursor = exc.__traceback__
        while cursor is not None:
            if cursor.tb_frame.f_code.co_name == "test_real_agent_loop_grants_one_semantic_recovery_then_pauses":
                task = cursor.tb_frame.f_locals.get("task") or {}
                diagnostic = {
                    "agent_cycle": task.get("agent_cycle"),
                    "agent_status": task.get("agent_status"),
                    "progress": task.get("agent_progress_state"),
                    "controller": task.get("agent_stagnation_controller"),
                    "lease": task.get("agent_stagnation_recovery_lease"),
                    "recovery_history": task.get("agent_stagnation_recovery_history"),
                    "guidance_count": len(task.get("guidance_messages") or []),
                    "events": [
                        {
                            "type": item.get("type"),
                            "cycle": item.get("cycle"),
                            "kind": item.get("intervention_kind"),
                            "summary": item.get("summary"),
                        }
                        for item in (task.get("agent_events") or [])
                        if item.get("type") in {
                            "investigation_checkpoint",
                            "stagnation_intervention",
                            "investigation_checkpoint_error",
                            "no_progress_recovery",
                            "no_progress_limit",
                        }
                    ],
                }
                print("STAGNATION_DEBUG=" + json.dumps(diagnostic, sort_keys=True, default=str))
                break
            cursor = cursor.tb_next
        raise
