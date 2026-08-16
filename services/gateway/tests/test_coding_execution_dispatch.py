from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app import coding_execution_dispatch as dispatch
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        function=ToolFunction(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
        )
    )


class _UserLLM:
    @staticmethod
    def is_user_model_id(_model: str) -> bool:
        return False


class _Admission:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, backend: str, _capability: str) -> None:
        self.released.append(backend)


class _Agent:
    ChatMessage = ChatMessage
    user_llm = _UserLLM()

    def __init__(self) -> None:
        self.admission = _Admission()
        self.calls: list[tuple[str, ChatCompletionRequest]] = []
        self.events: list[dict] = []
        self.task: dict = {}

        class _Forced:
            @staticmethod
            def active_state(task):
                return dict(task.get("forced") or {})

        self.forced_action = _Forced()

    @staticmethod
    def _backend_supports_tool_calling(backend: str) -> bool:
        return backend == "native"

    @staticmethod
    def _tool_specs_for_task(task: dict):
        return [_tool(name) for name in task.get("allowed_tools", [])]

    @staticmethod
    def _system_prompt(task: dict, *, text_tool_mode: bool = False) -> str:
        mode = "text" if text_tool_mode else "native"
        return (
            "You are Nexus Coding Agent. "
            f"action={task.get('forced', {}).get('action_kind', '')}; mode={mode}; "
            f"allowed={','.join(task.get('allowed_tools', []))}"
        )

    @staticmethod
    def _max_completion_tokens_for_route(_model: str, backend: str, _upstream: str) -> int:
        return 256 if backend == "text" else 1024

    @staticmethod
    def _backend_retry_count() -> int:
        return 1

    def get_admission_controller(self):
        return self.admission

    @staticmethod
    def _is_retryable_backend_error(_exc: HTTPException) -> bool:
        return True

    @staticmethod
    def _backend_retry_delay(_attempt: int) -> float:
        return 0.0

    @staticmethod
    def _clip_text(value: str, limit: int) -> str:
        return value[:limit]

    def _append_event(self, _task_id: str, event: dict) -> None:
        self.events.append(dict(event))

    def _mutate_task(self, _task_id: str, updates: dict) -> None:
        self.task.update(updates)

    async def call_backend_chat(
        self,
        req: ChatCompletionRequest,
        backend: str,
        _upstream_model: str,
    ):
        self.calls.append((backend, req))
        if backend == "native":
            raise HTTPException(
                status_code=502,
                detail={"error": "ReadTimeout: read timeout after 600s"},
            )
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class _CW:
    def __init__(self, task: dict) -> None:
        self.task = task

    def load_task(self, _task_id: str):
        return self.task


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="coder",
        messages=[
            ChatMessage(role="system", content="You are Nexus Coding Agent. action=evidence"),
            ChatMessage(role="user", content="work"),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "coding_read_file_lines",
                            "arguments": '{"path":"app.py"}',
                        },
                    }
                ],
            ),
            ChatMessage(role="tool", tool_call_id="call-1", content='{"path":"app.py"}'),
            ChatMessage(role="assistant", content=""),
        ],
        tools=[_tool("coding_read_file_lines")],
        tool_choice="auto",
        max_tokens=1024,
    )


def test_materialization_refreshes_policy_and_converts_native_history_for_text_backend():
    agent = _Agent()
    task = {
        "forced": {"state_key": "state-1", "action_kind": "edit"},
        "allowed_tools": ["coding_apply_patch", "coding_finish"],
        "project_plan": {"revision": 2},
    }

    req, snapshot, diag = dispatch.materialize_request(
        agent,
        _request(),
        task,
        source_backend="native",
        backend="text",
        upstream_model="devstral",
    )

    assert snapshot.action_kind == "edit"
    assert snapshot.text_tool_mode is True
    assert req.tools is None
    assert req.tool_choice is None
    assert req.max_tokens == 256
    assert "action=edit" in req.messages[0].content
    assert not any(message.role == "tool" for message in req.messages)
    assert not any(
        message.role == "assistant" and not str(message.content or "").strip()
        for message in req.messages
    )
    assistant_text = "\n".join(
        str(message.content or "") for message in req.messages if message.role == "assistant"
    )
    user_text = "\n".join(
        str(message.content or "") for message in req.messages if message.role == "user"
    )
    assert "<tool_call>" in assistant_text
    assert "coding_read_file_lines" in assistant_text
    assert "Tool result for coding_read_file_lines" in user_text
    assert diag["removed_empty_assistant_messages"] == 1
    assert diag["converted_tool_calls"] == 1
    assert diag["converted_tool_results"] == 1


def test_materialization_refreshes_native_tool_allowlist_after_controller_transition():
    agent = _Agent()
    task = {
        "forced": {"state_key": "state-1", "action_kind": "edit"},
        "allowed_tools": ["coding_apply_patch", "coding_finish"],
        "project_plan": {"revision": 3},
    }

    req, snapshot, _diag = dispatch.materialize_request(
        agent,
        _request(),
        task,
        source_backend="native",
        backend="native",
        upstream_model="glm",
    )

    assert "action=edit" in req.messages[0].content
    assert [spec.function.name for spec in req.tools or []] == [
        "coding_apply_patch",
        "coding_finish",
    ]
    assert snapshot.plan_revision == 3


def test_full_read_timeout_failover_rematerializes_for_destination_tool_protocol():
    agent = _Agent()
    task = {
        "forced": {"state_key": "state-1", "action_kind": "edit"},
        "allowed_tools": ["coding_apply_patch", "coding_finish"],
        "project_plan": {"revision": 2},
    }
    agent.task = task
    cw = _CW(task)

    class _Guarded:
        _agent = agent
        _ORIGINAL_CALL_BACKEND_CHAT_WITH_RETRY = None

        @staticmethod
        async def _acquire_backend_excluding(
            _model,
            _preferred_backend,
            _preferred_upstream,
            *,
            task_id,
            cycle,
            attempt,
            excluded_backends,
        ):
            if "native" in excluded_backends:
                return {
                    "backend": "text",
                    "upstream_model": "devstral",
                    "host": "stackrot",
                    "ready": True,
                    "available": 1,
                }
            return {
                "backend": "native",
                "upstream_model": "glm",
                "host": "ai2",
                "ready": True,
                "available": 1,
            }

    call = dispatch.build_failover_call(cw, _Guarded)
    response, backend, model = asyncio.run(
        call(
            _request(),
            "native",
            "glm",
            task_id="code-test",
            cycle=10,
        )
    )

    assert response["choices"][0]["message"]["content"] == "ok"
    assert backend == "text"
    assert model == "devstral"
    assert [item[0] for item in agent.calls] == ["native", "text"]
    text_request = agent.calls[-1][1]
    assert text_request.tools is None
    assert not any(message.role == "tool" for message in text_request.messages)
    assert agent.admission.released == ["native", "text"]
