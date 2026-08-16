from __future__ import annotations

from app import coding_execution_dispatch as dispatch
from app.models import ChatMessage


class _Agent:
    ChatMessage = ChatMessage

    @staticmethod
    def _tool_context_char_limit() -> int:
        return 12_000

    @staticmethod
    def _clip_text(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)] + "…"


def test_text_failover_clips_large_native_tool_results() -> None:
    messages = [
        ChatMessage(role="system", content="You are Nexus Coding Agent."),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "coding_read_file_lines",
                        "arguments": '{"path":"large.py"}',
                    },
                }
            ],
        ),
        ChatMessage(
            role="tool",
            tool_call_id="call-1",
            content="x" * 20_000,
        ),
    ]

    normalized, diagnostics = dispatch._normalize_messages(
        _Agent(),
        messages,
        text_tool_mode=True,
        fresh_system="You are Nexus Coding Agent. text mode",
    )

    converted = [message for message in normalized if message.role == "user"]
    assert len(converted) == 1
    content = str(converted[0].content or "")
    assert len(content) < 1_100
    assert "…\n\nContinue the coding task" in content
    assert content.endswith(
        "or call coding_finish when the task is complete or blocked."
    )
    assert diagnostics["converted_tool_results"] == 1
    assert diagnostics["clipped_tool_results"] == 1
