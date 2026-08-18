from __future__ import annotations

from app import coding_text_tool_handoff as handoff
from app.models import ToolFunction, ToolSpec


def _tool(name: str, properties: dict) -> ToolSpec:
    return ToolSpec(
        function=ToolFunction(
            name=name,
            description=f"Use {name} in the shared coding workspace.",
            parameters={
                "type": "object",
                "properties": properties,
                "required": list(properties),
            },
        )
    )


class _Agent:
    def __init__(self) -> None:
        self._text_tool_handoff_installed = False

    @staticmethod
    def _system_prompt(task, *, text_tool_mode=False):
        return f"base:{task.get('mode', '')}:{'text' if text_tool_mode else 'native'}"

    @staticmethod
    def _max_completion_tokens_for_route(_model, _backend, _upstream_model=""):
        return 64

    @staticmethod
    def _backend_supports_tool_calling(backend):
        return backend == "local_mlx"

    @staticmethod
    def _tool_specs_for_task(task):
        specs = {
            "coding_apply_patch": _tool(
                "coding_apply_patch",
                {"patch": {"type": "string"}, "check_only": {"type": "boolean"}},
            ),
            "coding_finish": _tool(
                "coding_finish",
                {"summary": {"type": "string"}, "success": {"type": "boolean"}},
            ),
            "coding_search_text": _tool(
                "coding_search_text",
                {"query": {"type": "string"}, "path": {"type": "string"}},
            ),
        }
        return [specs[name] for name in task.get("allowed_tools", [])]


def test_text_tool_prompt_carries_exact_current_workspace_contracts():
    agent = _Agent()
    handoff.install(agent)
    task = {
        "mode": "edit",
        "allowed_tools": ["coding_apply_patch", "coding_finish"],
    }

    prompt = agent._system_prompt(task, text_tool_mode=True)

    assert "Text-tool workspace contracts" in prompt
    assert '"name":"coding_apply_patch"' in prompt
    assert '"patch":{"type":"string"}' in prompt
    assert '"check_only":{"type":"boolean"}' in prompt
    assert '"name":"coding_finish"' in prompt
    assert "coding_search_text" not in prompt
    assert "same repository" in prompt
    assert "durable project plan" in prompt


def test_native_tool_prompt_is_unchanged():
    agent = _Agent()
    original = agent._system_prompt({"mode": "edit"}, text_tool_mode=False)
    handoff.install(agent)

    assert agent._system_prompt({"mode": "edit"}, text_tool_mode=False) == original


def test_devstral_text_tool_route_has_budget_to_emit_real_edits():
    agent = _Agent()
    handoff.install(agent)

    cap = agent._max_completion_tokens_for_route(
        "coder",
        "local_vllm_fast",
        "cyankiwi/Devstral-Small-2507-AWQ-4bit",
    )

    assert cap == 2048
    assert cap > 64


def test_native_route_keeps_original_completion_budget():
    agent = _Agent()
    handoff.install(agent)

    assert agent._max_completion_tokens_for_route("coder", "local_mlx", "glm") == 64


def test_text_tool_handoff_install_is_idempotent():
    agent = _Agent()
    handoff.install(agent)
    first_prompt = agent._system_prompt
    first_tokens = agent._max_completion_tokens_for_route

    handoff.install(agent)

    assert agent._system_prompt is first_prompt
    assert agent._max_completion_tokens_for_route is first_tokens
