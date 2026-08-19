from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from app import coding_plan_edit_serialization as serialization


class Workspace:
    def __init__(self):
        self.plan_calls = []

    def update_project_plan(self, task_id, *, goal=None, items=None, note=None, actor=None):
        self.plan_calls.append((task_id, goal, note))
        return {"ok": True}


def test_plan_update_cannot_enter_between_edit_revalidation_wrapper_and_mutation():
    workspace = Workspace()
    edit_entered = threading.Event()
    release_edit = threading.Event()
    edit_finished = threading.Event()
    calls = []

    def run_tool(task_id, name, args, *, git_token_value):
        calls.append(("edit_enter", task_id, name))
        edit_entered.set()
        assert release_edit.wait(timeout=2)
        calls.append(("edit_exit", task_id, name))
        edit_finished.set()
        return {"ok": True}

    agent = SimpleNamespace(cw=workspace, _run_tool=run_tool)
    serialization.install(agent)

    edit_thread = threading.Thread(
        target=lambda: agent._run_tool(
            "code_lock",
            "coding_apply_patch",
            {"patch": "x"},
            git_token_value=None,
        )
    )
    edit_thread.start()
    assert edit_entered.wait(timeout=2)

    plan_done = threading.Event()

    def update_plan():
        workspace.update_project_plan("code_lock", goal="concurrent update")
        plan_done.set()

    plan_thread = threading.Thread(target=update_plan)
    plan_thread.start()
    time.sleep(0.05)
    assert not plan_done.is_set()
    assert workspace.plan_calls == []

    release_edit.set()
    assert edit_finished.wait(timeout=2)
    edit_thread.join(timeout=2)
    plan_thread.join(timeout=2)

    assert plan_done.is_set()
    assert workspace.plan_calls == [("code_lock", "concurrent update", None)]
    assert calls == [
        ("edit_enter", "code_lock", "coding_apply_patch"),
        ("edit_exit", "code_lock", "coding_apply_patch"),
    ]


def test_different_workspaces_do_not_block_each_other():
    workspace = Workspace()
    started = threading.Event()
    release = threading.Event()

    def run_tool(task_id, name, args, *, git_token_value):
        if task_id == "code_a":
            started.set()
            assert release.wait(timeout=2)
        return {"ok": True}

    agent = SimpleNamespace(cw=workspace, _run_tool=run_tool)
    serialization.install(agent)

    thread = threading.Thread(
        target=lambda: agent._run_tool(
            "code_a",
            "coding_apply_patch",
            {},
            git_token_value=None,
        )
    )
    thread.start()
    assert started.wait(timeout=2)

    # A plan update for another workspace has a different lock and proceeds.
    result = workspace.update_project_plan("code_b", goal="independent")
    assert result["ok"] is True
    assert workspace.plan_calls == [("code_b", "independent", None)]

    release.set()
    thread.join(timeout=2)
