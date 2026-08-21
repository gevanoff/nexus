from __future__ import annotations

from types import SimpleNamespace

from app import coding_completion_state_hardening as hardening
from app import coding_execution_dispatch as dispatch
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec


NOTE = (
    "Root cause: management metadata is suppressed by a backend guard.\n"
    "Repository evidence: services/gateway/app/ui_routes.py:1640-1699\n"
    "Competing explanation checked: frontend rendering exists.\n"
    "Expected result: restore the backend management link."
)
PY_COMPILE = ["python3", "-m", "py_compile", "services/gateway/app/ui_routes.py"]


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        function=ToolFunction(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
        )
    )


def _validation_task(*, recovery: str = "same") -> dict:
    """Production-shaped edit -> failed validation -> later validation sequence."""
    success_argv = PY_COMPILE if recovery == "same" else ["git", "diff", "--check"]
    return {
        "id": "code-test",
        "agent_run_id": "coderun-test",
        "project_plan": {"revision": 2, "note": NOTE, "items": []},
        "agent_events": [
            {"type": "started", "run_id": "coderun-test"},
            {
                "type": "tool_started",
                "cycle": 4,
                "tool_call_id": "edit-1",
                "name": "coding_replace_text",
                "args": {"path": "services/gateway/app/ui_routes.py"},
            },
            {
                "type": "tool_finished",
                "cycle": 4,
                "tool_call_id": "edit-1",
                "name": "coding_replace_text",
                "result": {"ok": True, "replacements": 1},
            },
            {
                "type": "tool_started",
                "cycle": 5,
                "tool_call_id": "validate-1",
                "name": "coding_run_command",
                "args": {"argv": PY_COMPILE, "cwd": "services/gateway"},
            },
            {
                "type": "tool_finished",
                "cycle": 5,
                "tool_call_id": "validate-1",
                "name": "coding_run_command",
                "result": {
                    "ok": False,
                    "stderr": "can't open file services/gateway/app/ui_routes.py",
                },
            },
            {
                "type": "tool_started",
                "cycle": 6,
                "tool_call_id": "validate-2",
                "name": "coding_run_command",
                "args": {"argv": success_argv},
            },
            {
                "type": "tool_finished",
                "cycle": 6,
                "tool_call_id": "validate-2",
                "name": "coding_run_command",
                "result": {"ok": True, "stdout": "", "stderr": ""},
            },
        ],
    }


class _CW:
    def __init__(self, *, validation_ready: bool = False, review_ready: bool = False):
        self.validation_ready = validation_ready
        self.review_ready = review_ready
        self.tasks: dict[str, dict] = {}

    def coding_state_snapshot(self, _task_id: str):
        return {
            "validation": {
                "validation_after_latest_edit": self.validation_ready,
                "last_validation_ok": self.validation_ready,
            },
            "diff_review": {
                "diff_reviewed_after_latest_edit": self.review_ready,
            },
        }

    def workspace_progress_fingerprint(self, _task_id: str):
        return "workspace-after-edit"

    def load_task(self, task_id: str):
        return self.tasks[task_id]


def test_same_validation_rerun_supersedes_earlier_invocation_failure():
    cw = _CW()
    task = _validation_task()
    cw.tasks["code-test"] = task

    assert hardening._validation_recovery_state(task) == (True, True)
    updated = hardening._finish_gate_overrides(
        cw,
        task,
        {
            "validation_run_after_edit": True,
            "validation_ok_after_edit": True,
            "validation_failed_after_edit": True,
            "diff_reviewed_after_edit": True,
        },
    )

    assert updated["validation_failed_after_edit"] is False
    assert updated["validation_run_after_edit"] is True
    assert updated["validation_ok_after_edit"] is True


def test_later_weak_check_does_not_supersede_stronger_failed_validation():
    cw = _CW()
    task = _validation_task(recovery="weak")
    cw.tasks["code-test"] = task

    assert hardening._validation_recovery_state(task) == (True, False)
    updated = hardening._finish_gate_overrides(
        cw,
        task,
        {
            "validation_run_after_edit": True,
            "validation_ok_after_edit": True,
            "validation_failed_after_edit": True,
            "diff_reviewed_after_edit": True,
        },
    )

    assert updated["validation_failed_after_edit"] is True


def test_durable_current_state_reconciles_stale_local_flags_after_exact_rerun():
    cw = _CW(validation_ready=True, review_ready=True)
    task = _validation_task()
    cw.tasks["code-test"] = task
    updated = hardening._finish_gate_overrides(
        cw,
        task,
        {
            "validation_run_after_edit": False,
            "validation_ok_after_edit": None,
            "validation_failed_after_edit": True,
            "diff_reviewed_after_edit": False,
        },
    )

    assert updated["validation_failed_after_edit"] is False
    assert updated["validation_run_after_edit"] is True
    assert updated["validation_ok_after_edit"] is True
    assert updated["diff_reviewed_after_edit"] is True


def _consumed_task() -> dict:
    return {
        "id": "code-test",
        "agent_run_id": "coderun-test",
        "project_plan": {"revision": 2, "note": NOTE, "items": []},
        hardening._LIFECYCLE_KEY: {
            "schema": "nexus_coding_hypothesis_lifecycle.v1",
            "status": "consumed",
            "plan_revision": 2,
            "note_fingerprint": hardening._note_fingerprint(NOTE),
            "verified_evidence_digest": (
                "services/gateway/app/ui_routes.py:1640-1699\n"
                "services/gateway/app/backends_config.yaml:70-95"
            ),
        },
    }


def test_consumed_hypothesis_is_historical_until_plan_revision_changes():
    task = _consumed_task()

    historical = hardening._historical_task(task)
    assert "HISTORICAL CONSUMED REMEDIATION HYPOTHESIS" in historical["project_plan"]["note"]
    assert "not current causal truth" in hardening._lifecycle_context(task)
    assert "backends_config.yaml:70-95" in hardening._lifecycle_context(task)

    refreshed = dict(task)
    refreshed["project_plan"] = {"revision": 3, "note": NOTE, "items": []}
    assert hardening._matching_consumed_lifecycle(refreshed) == {}
    assert hardening._historical_task(refreshed) is refreshed


class _Persistence:
    @staticmethod
    def _verified_evidence_digest(_task, _state):
        return "Verified repository evidence: services/gateway/app/ui_routes.py:1640-1699"


class _LifecycleAgent:
    def __init__(self, cw: _CW):
        self.cw = cw
        self.events: list[dict] = []

    def _mutate_task(self, task_id: str, updates: dict):
        self.cw.tasks[task_id].update(updates)

    def _append_event(self, _task_id: str, event: dict):
        self.events.append(dict(event))


def test_repository_mutation_records_consumed_hypothesis_with_evidence_snapshot():
    cw = _CW()
    task = {
        "id": "code-test",
        "agent_run_id": "coderun-test",
        "project_plan": {"revision": 2, "note": NOTE},
    }
    cw.tasks["code-test"] = task
    agent = _LifecycleAgent(cw)
    state = {
        "causal_evidence_targets": ["services/gateway/app/ui_routes.py"],
        "causal_evidence_ranges": [
            {
                "path": "services/gateway/app/ui_routes.py",
                "start_line": 1640,
                "end_line": 1699,
            }
        ],
    }

    hardening._record_consumed_hypothesis(
        agent,
        cw,
        _Persistence,
        task_id="code-test",
        before_task=task,
        before_state=state,
        tool_name="coding_replace_text",
    )

    lifecycle = cw.tasks["code-test"][hardening._LIFECYCLE_KEY]
    assert lifecycle["status"] == "consumed"
    assert lifecycle["plan_revision"] == 2
    assert lifecycle["workspace_fingerprint_after"] == "workspace-after-edit"
    assert lifecycle["causal_evidence_ranges"][0]["end_line"] == 1699
    assert "Verified repository evidence" in lifecycle["verified_evidence_digest"]
    assert agent.events[-1]["type"] == "hypothesis_consumed"


class _ProtocolAgent:
    ChatMessage = ChatMessage

    @staticmethod
    def _system_prompt(_task, *, text_tool_mode=False):
        return f"You are Nexus Coding Agent. text_tool_mode={text_tool_mode}"

    @staticmethod
    def _tool_context_char_limit():
        return 4000

    @staticmethod
    def _clip_text(value, limit):
        text = str(value)
        return text if len(text) <= limit else text[:limit]


def _native_history_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="coder",
        messages=[
            ChatMessage(role="system", content="fresh text-backend system"),
            ChatMessage(role="user", content="perform the edit"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "coding_read_file_lines",
                            "arguments": '{"path":"app.py","start_line":1,"line_count":10}',
                        },
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                tool_call_id="call-1",
                content='{"ok":true,"path":"app.py"}',
            ),
        ],
        tools=[_tool("coding_read_file_lines")],
        tool_choice="auto",
        max_tokens=1024,
    )


def test_text_backend_transport_repair_removes_native_tool_protocol():
    req = _native_history_request()
    assert hardening._text_tool_protocol_violations(dispatch, req) == [
        "role_tool_message",
        "native_tool_calls",
        "native_tools_schema",
        "native_tool_choice",
    ]

    repaired, diagnostics = hardening._repair_text_tool_transport(
        _ProtocolAgent(),
        dispatch,
        req,
        {},
    )

    assert hardening._text_tool_protocol_violations(dispatch, repaired) == []
    assert repaired.tools is None
    assert repaired.tool_choice is None
    assert not any(message.role == "tool" for message in repaired.messages)
    assert repaired.messages[0].content == "fresh text-backend system"
    assistant_text = "\n".join(
        str(message.content or "")
        for message in repaired.messages
        if message.role == "assistant"
    )
    user_text = "\n".join(
        str(message.content or "")
        for message in repaired.messages
        if message.role == "user"
    )
    assert "<tool_call>" in assistant_text
    assert "Tool result for coding_read_file_lines" in user_text
    assert diagnostics["converted_tool_calls"] == 1
    assert diagnostics["converted_tool_results"] == 1


class _Forced:
    @staticmethod
    def active_state(task):
        return dict(task.get("forced") or {})


class _InstallAgent(_ProtocolAgent):
    def __init__(self, cw: _CW):
        self.cw = cw
        self.forced_action = _Forced()
        self.events: list[dict] = []
        self._finish_gate_feedback = self._finish
        self._run_tool = self._run
        self._task_context = lambda task: str(task.get("project_plan", {}).get("note") or "")
        self._text_tool_task_context = self._task_context

    @staticmethod
    def _finish(*_args, **kwargs):
        if kwargs.get("validation_failed_after_edit"):
            return "failed validation"
        if not kwargs.get("validation_run_after_edit"):
            return "missing validation"
        if not kwargs.get("diff_reviewed_after_edit"):
            return "missing diff"
        return ""

    @staticmethod
    def _tool_result_modified_workspace(name, _args, result):
        return name == "coding_replace_text" and result.get("ok") is True

    @staticmethod
    def _run(_task_id, name, _args, *, git_token_value):
        assert git_token_value is None
        if name == "coding_replace_text":
            return {"ok": True, "replacements": 1}
        return {"ok": True}

    def _mutate_task(self, task_id: str, updates: dict):
        self.cw.tasks[task_id].update(updates)

    def _append_event(self, _task_id: str, event: dict):
        self.events.append(dict(event))


class _Guarded:
    def __init__(self, agent):
        self._run_tool_with_semantic_acceptance = agent._run_tool
        self._project_hypothesis_text = lambda task: str(
            task.get("project_plan", {}).get("note") or ""
        )


class _Semantic:
    @staticmethod
    def build_review_messages(**_kwargs):
        return "base system", "base user"


class _Policy:
    @staticmethod
    def execution_task(_agent, task):
        return task


class _Dispatch:
    coding_execution_policy = _Policy
    _request_value = staticmethod(dispatch._request_value)
    _normalize_messages = staticmethod(dispatch._normalize_messages)
    _copy_request = staticmethod(dispatch._copy_request)

    def __init__(self):
        self.materialize_request = self._materialize

    @staticmethod
    def _materialize(_agent, req, _task, *, source_backend, backend, upstream_model):
        del source_backend, backend, upstream_model
        return req, SimpleNamespace(text_tool_mode=True), {"coding_request": True}


def test_install_closes_finish_transport_and_hypothesis_lifecycle_loops():
    cw = _CW(validation_ready=True, review_ready=True)
    task = _validation_task()
    lifecycle = _consumed_task()[hardening._LIFECYCLE_KEY]
    task[hardening._LIFECYCLE_KEY] = lifecycle
    task["forced"] = {
        "causal_evidence_targets": ["services/gateway/app/ui_routes.py"],
        "causal_evidence_ranges": [
            {
                "path": "services/gateway/app/ui_routes.py",
                "start_line": 1640,
                "end_line": 1699,
            }
        ],
    }
    cw.tasks["code-test"] = task

    agent = _InstallAgent(cw)
    guarded = _Guarded(agent)
    semantic = SimpleNamespace(build_review_messages=_Semantic.build_review_messages)
    execution_dispatch = _Dispatch()

    hardening.install(
        agent,
        guarded,
        cw,
        execution_dispatch,
        _Persistence,
        semantic,
    )

    feedback = agent._finish_gate_feedback(
        task=task,
        finish_success=True,
        workspace_modified=True,
        validation_run_after_edit=False,
        validation_ok_after_edit=None,
        validation_failed_after_edit=True,
        diff_reviewed_after_edit=False,
    )
    assert feedback == ""

    assert "HISTORICAL CONSUMED" in agent._task_context(task)
    review_context = guarded._project_hypothesis_text(task)
    assert "Hypothesis lifecycle: consumed" in review_context
    assert "Verified pre-edit repository evidence snapshot" in review_context
    assert "backends_config.yaml:70-95" in review_context
    system, _user = semantic.build_review_messages()
    assert "Missing evidence is not evidence of absence" in system

    materialized, _snapshot, diag = execution_dispatch.materialize_request(
        agent,
        _native_history_request(),
        task,
        source_backend="native",
        backend="text",
        upstream_model="devstral",
    )
    assert hardening._text_tool_protocol_violations(dispatch, materialized) == []
    assert diag["transport_invariant_repaired"] is True

    fresh_task = {
        "id": "code-fresh",
        "agent_run_id": "coderun-fresh",
        "project_plan": {"revision": 4, "note": NOTE},
        "forced": task["forced"],
    }
    cw.tasks["code-fresh"] = fresh_task
    result = agent._run_tool(
        "code-fresh",
        "coding_replace_text",
        {"path": "app.py", "old_text": "old", "new_text": "new"},
        git_token_value=None,
    )
    assert result["ok"] is True
    assert cw.tasks["code-fresh"][hardening._LIFECYCLE_KEY]["status"] == "consumed"
    assert agent.events[-1]["type"] == "hypothesis_consumed"
