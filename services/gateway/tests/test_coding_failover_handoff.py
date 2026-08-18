from __future__ import annotations

from app import coding_execution_dispatch as dispatch
from app import coding_text_tool_handoff as handoff
from app import upstreams
from app.models import ChatCompletionRequest, ChatMessage, ToolFunction, ToolSpec
from app.prompt_canonicalization import canonicalize_chat_payload


def _tool(name: str, properties: dict) -> ToolSpec:
    return ToolSpec(
        function=ToolFunction(
            name=name,
            description=f"Coding workspace tool {name}",
            parameters={
                "type": "object",
                "properties": properties,
                "required": list(properties),
            },
        )
    )


class _Forced:
    @staticmethod
    def active_state(task):
        return dict(task.get("forced") or {})


class _Agent:
    ChatMessage = ChatMessage

    def __init__(self) -> None:
        self.forced_action = _Forced()

    @staticmethod
    def _backend_supports_tool_calling(backend: str) -> bool:
        return backend == "local_mlx"

    @staticmethod
    def _tool_specs_for_task(task: dict):
        available = {
            "coding_apply_patch": _tool(
                "coding_apply_patch",
                {"patch": {"type": "string"}, "check_only": {"type": "boolean"}},
            ),
            "coding_finish": _tool(
                "coding_finish",
                {"summary": {"type": "string"}, "success": {"type": "boolean"}},
            ),
        }
        return [available[name] for name in task.get("allowed_tools", [])]

    def _system_prompt(self, task: dict, *, text_tool_mode: bool = False) -> str:
        mode = "text" if text_tool_mode else "native"
        return (
            "You are Nexus Coding Agent. "
            f"mode={mode}; action={task.get('forced', {}).get('action_kind', '')}."
        )

    @staticmethod
    def _max_completion_tokens_for_route(_model: str, _backend: str, _upstream: str = "") -> int:
        # Reproduce the production text-tool default before the handoff shim.
        return 64

    @staticmethod
    def _compact_text_tool_messages(messages):
        return list(messages)

    @staticmethod
    def _tool_context_char_limit() -> int:
        return 12_000

    @staticmethod
    def _clip_text(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"


def _native_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="coder",
        messages=[
            ChatMessage(role="system", content="You are Nexus Coding Agent. native"),
            ChatMessage(role="user", content="Restore the InvokeAI link."),
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "coding_read_file_lines",
                            "arguments": '{"path":"services/gateway/app/static/image.js","start_line":60,"line_count":40}',
                        },
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                tool_call_id="call-1",
                content='{"path":"services/gateway/app/static/image.js","content":"function safeExternalUrl(...)"}',
            ),
            ChatMessage(role="user", content="Controller: make the smallest evidence-backed edit."),
            ChatMessage(role="assistant", content=""),
        ],
        tools=[
            _tool(
                "coding_read_file_lines",
                {"path": {"type": "string"}, "start_line": {"type": "integer"}},
            )
        ],
        tool_choice="auto",
        max_tokens=16_384,
    )


def _final_openai_payload(req: ChatCompletionRequest) -> dict:
    payload = req.model_dump(exclude_none=True)
    payload.pop("x_nexus", None)
    payload["messages"] = upstreams._normalize_messages_for_openai_backend(payload["messages"])
    payload = upstreams._normalize_openai_tools_payload(payload, include_strict=False)
    return canonicalize_chat_payload(payload)


def test_mlx_to_devstral_handoff_is_executable_at_final_transport_boundary():
    agent = _Agent()
    handoff.install(agent)
    task = {
        "forced": {"state_key": "state-1", "action_kind": "edit"},
        "allowed_tools": ["coding_apply_patch", "coding_finish"],
        "project_plan": {"revision": 2},
    }

    adapted, snapshot, diagnostics = dispatch.materialize_request(
        agent,
        _native_request(),
        task,
        source_backend="local_mlx",
        backend="local_vllm_fast",
        upstream_model="cyankiwi/Devstral-Small-2507-AWQ-4bit",
    )
    payload = _final_openai_payload(adapted)

    assert snapshot.text_tool_mode is True
    assert snapshot.action_kind == "edit"
    assert adapted.max_tokens == 2048
    assert diagnostics["converted_tool_calls"] == 1
    assert diagnostics["converted_tool_results"] == 1
    assert diagnostics["removed_empty_assistant_messages"] == 1

    messages = payload["messages"]
    assert not any(message.get("role") == "tool" for message in messages)
    assert not any(
        message.get("role") == "assistant"
        and not str(message.get("content") or "").strip()
        and not message.get("tool_calls")
        for message in messages
    )
    assert any(
        message.get("role") == "assistant"
        and "<tool_call>" in str(message.get("content") or "")
        and "coding_read_file_lines" in str(message.get("content") or "")
        for message in messages
    )
    assert any(
        message.get("role") == "user"
        and "Tool result for coding_read_file_lines" in str(message.get("content") or "")
        for message in messages
    )

    system = next(message["content"] for message in messages if message.get("role") == "system")
    assert "Text-tool workspace contracts" in system
    assert '"name":"coding_apply_patch"' in system
    assert '"patch":{"type":"string"}' in system
    assert '"name":"coding_finish"' in system
    assert "coding_read_file_lines" not in system.split("Contracts JSON:", 1)[1]


def test_final_transport_keeps_real_native_tool_call_but_never_empty_assistant_bridge():
    req = ChatCompletionRequest(
        model="devstral",
        messages=[
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "coding_finish", "arguments": "{}"},
                    }
                ],
            ),
            ChatMessage(role="tool", tool_call_id="call-1", content="ok"),
            ChatMessage(role="user", content="continue"),
        ],
    )

    payload = _final_openai_payload(req)

    assert payload["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert not any(
        message.get("role") == "assistant"
        and not str(message.get("content") or "").strip()
        and not message.get("tool_calls")
        for message in payload["messages"]
    )
    bridge = payload["messages"][2]
    assert bridge["role"] == "assistant"
    assert bridge["content"].strip()
