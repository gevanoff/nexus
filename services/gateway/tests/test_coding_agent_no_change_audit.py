from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_agent as ca


def test_no_change_audit_fails_finish_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="Completed requested work.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is False
    assert "called coding_finish" in summary
    assert event is not None
    assert event["type"] == "no_change_audit"


def test_no_change_audit_allows_answer_only_run_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="The workspace is currently clean and no changes are needed.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
        expects_workspace_edits=False,
    )

    assert success is True
    assert summary == "The workspace is currently clean and no changes are needed."
    assert event is None


def test_no_change_audit_fails_unfinished_run_without_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=False,
        finish_success=False,
        finish_summary="Run paused before the agent called coding_finish.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is False
    assert "No-change audit" in summary
    assert event is not None
    assert event["type"] == "no_change_audit"


def test_no_change_audit_preserves_runs_with_edits():
    success, summary, event = ca._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="Completed requested work.",
        committed_changes=False,
        uncommitted_changes=True,
        start_head="abc123",
        end_head="abc123",
    )

    assert success is True
    assert summary == "Completed requested work."
    assert event is None


def test_incomplete_text_tool_call_detection_flags_unclosed_blocks():
    assert ca._has_incomplete_text_tool_call('<tool_call>{"name":"coding_read_file_lines"') is True
    assert ca._has_incomplete_text_tool_call('<tool_call><function=coding_git_diff></function></tool_call>') is False


def test_extract_text_tool_calls_parses_complete_json_block():
    calls = ca._extract_text_tool_calls(
        '<tool_call>{"name":"coding_read_file_lines","arguments":{"path":"README.md","start_line":1,"line_count":20}}</tool_call>'
    )

    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "coding_read_file_lines"


def test_text_tool_mode_prompt_and_results_use_plain_messages():
    task = {"id": "code_test", "prompt": "Fix it.", "base_branch": "main", "branch_name": "nexus-coder/code_test"}
    prompt = ca._system_prompt(
        task,
        text_tool_mode=True,
    )
    context = ca._text_tool_task_context(task)
    message = ca._text_tool_result_message(name="coding_git_status", result={"ok": True, "status": "clean"})

    assert "<tool_call>" in prompt
    assert "coding_tool_manifest" in prompt
    assert "Fix it." in prompt
    assert "Fix it." not in context
    assert message.role == "user"
    assert message.tool_call_id is None
    assert "Tool result for coding_git_status" in str(message.content)


def test_non_native_coding_route_uses_text_tool_token_cap(monkeypatch):
    class FakeBackend:
        provider = "vllm"
        payload_policy = {"supports_tool_calling": False}

    class FakeRegistry:
        def get_backend(self, backend_name: str):
            return FakeBackend()

    monkeypatch.setattr(ca, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(ca.S, "CODING_AGENT_MAX_TOKENS", 8192, raising=False)
    monkeypatch.setattr(ca.S, "CODING_AGENT_TEXT_TOOL_MAX_TOKENS", 192, raising=False)
    monkeypatch.setattr(
        ca,
        "get_aliases",
        lambda: {"default": SimpleNamespace(backend="local_vllm", upstream_model="qwen-default", tools=True, max_tokens_cap=None)},
    )

    assert ca._max_completion_tokens_for_route("default", "local_vllm") == 192


def test_text_tool_message_compaction_keeps_recent_tail():
    messages = [
        ca.ChatMessage(role="system", content="system"),
        ca.ChatMessage(role="user", content="start"),
        *[ca.ChatMessage(role="assistant" if i % 2 else "user", content=f"m{i}") for i in range(10)],
    ]

    compacted = ca._compact_text_tool_messages(messages)

    assert compacted[0].content == "system"
    assert compacted[1].content == "start"
    assert "history was omitted" in str(compacted[2].content)
    assert [item.content for item in compacted[-5:]] == [f"m{i}" for i in range(5, 10)]


def test_parse_tool_arguments_coerces_json_encoded_argv_array():
    args = ca._parse_tool_arguments('{"argv":"[\\"python\\",\\"-m\\",\\"unittest\\"]","timeout_sec":120}')

    assert args["argv"] == ["python", "-m", "unittest"]
    assert args["timeout_sec"] == 120


def test_parse_tool_arguments_accepts_command_list_as_argv():
    args = ca._parse_tool_arguments({"command": ["python", "-m", "unittest"]})

    assert args["argv"] == ["python", "-m", "unittest"]


def test_parse_tool_arguments_does_not_split_shell_argv_string():
    args = ca._parse_tool_arguments({"argv": "python -m unittest"})

    assert args["argv"] == "python -m unittest"


def test_fix_oriented_request_is_marked_edit_expected():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Debug this failing workflow and fix the root cause in the repo.",
    }

    assert ca._request_expects_workspace_edits(task) is True
    prompt = ca._system_prompt(task)
    assert "This request is fix-oriented." in prompt
    assert "Do not stop at diagnosis alone" in prompt


def test_review_request_does_not_get_fix_oriented_prompt():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
    }

    assert ca._request_expects_workspace_edits(task) is False
    prompt = ca._system_prompt(task)
    assert "This request is fix-oriented." not in prompt


def test_prompt_warns_against_invented_symbols_and_requires_validation():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Fix the broken coding workspace behavior.",
    }

    prompt = ca._system_prompt(task)

    assert "Do not invent imports" in prompt
    assert "avoid loading the same library multiple times" in prompt
    assert "validation and coding_git_diff" in prompt
    assert "Do not invent package.json files" in prompt
    assert "do not count as a fix" in prompt


def test_prompt_declares_linux_workspace_conventions():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Fix the broken coding workspace behavior.",
    }

    prompt = ca._system_prompt(task)
    context = ca._task_context(task)

    assert "execution environment is Linux" in prompt
    assert "Do not assume PowerShell" in prompt
    assert "set cwd to that service directory" in prompt
    assert "Execution environment: Linux workspace shell" in context
    assert "services/gateway" in context


def test_tool_manifest_guidance_mentions_linux_shell_and_service_cwd():
    manifest = ca.coding_tool_manifest()
    guidance = "\n".join(manifest["guidance"])

    assert "Linux workspace shell" in guidance
    assert "Do not assume PowerShell" in guidance
    assert "cwd=services/gateway" in guidance
    assert "Do not invent package.json files" in guidance
    assert "do not count as a fix" in guidance
    assert "coding_update_plan" in manifest["tool_names"]
    assert "milestones" in guidance


def test_run_horizon_helpers_bound_values_and_measure_messages(monkeypatch):
    monkeypatch.setattr(ca.S, "CODING_AGENT_MAX_RUNTIME_SEC", 21600, raising=False)
    monkeypatch.setattr(ca.S, "CODING_AGENT_CONTEXT_RESET_CYCLES", 12, raising=False)

    assert ca._max_cycles_per_run(None) == 1000
    assert ca._max_cycles_per_run(80) == 80
    assert ca._max_cycles_per_run(9999) == 1000
    assert ca._max_runtime_sec(1) == 60
    assert ca._context_reset_cycles(2) == 4
    assert ca._messages_char_count([ca.ChatMessage(role="user", content="hello")]) >= 5


def test_project_plan_context_survives_context_rebuild():
    task = {
        "id": "code_test",
        "prompt": "Implement a multi-step feature.",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "project_plan": {
            "goal": "Ship safely",
            "items": [
                {"id": "inspect", "title": "Inspect the code", "status": "completed"},
                {"id": "build", "title": "Implement the change", "status": "in_progress"},
            ],
        },
    }

    context = ca._task_context(task)
    prompt = ca._system_prompt(task)

    assert "Long-horizon project plan" in context
    assert "[completed] inspect" in context
    assert "coding_update_plan" in prompt


def test_semantic_reroute_candidate_uses_alternative_backend(monkeypatch):
    monkeypatch.setattr(
        ca,
        "_rank_coding_backend_candidates",
        lambda *args, **kwargs: [
            {"backend": "local_mlx", "upstream_model": "mlx-a", "ready": True, "available": 1},
            {"backend": "local_vllm", "upstream_model": "vllm-b", "ready": True, "available": 1},
        ],
    )

    candidate = ca._semantic_reroute_candidate("coder", "local_mlx", "mlx-a")

    assert candidate is not None
    assert candidate["backend"] == "local_vllm"


def test_compact_event_marks_unverified_assistant_output_and_deduplicates():
    task = {
        "agent_events": [
            {
                "type": "assistant",
                "content": "Repeated prose-only output.",
            }
        ]
    }

    event = ca._compact_event(
        task,
        {
            "type": "assistant",
            "content": "Repeated prose-only output.",
            "tool_calls": [],
        },
    )

    assert event["summary"] == "Unverified model output before any workspace tool executed."
    assert event["content"] == "(same unverified model output as previous cycle)"


def test_workspace_chat_question_does_not_inherit_edit_expectation():
    task = {
        "id": "code_test",
        "base_branch": "main",
        "branch_name": "nexus-coder/code_test",
        "prompt": "Debug this failing workflow and fix the root cause in the repo.",
        "agent_run_prompt": "What changed in the last run?",
    }

    assert ca._request_expects_workspace_edits(task) is False


def test_model_is_reroutable_for_aliases_but_not_explicit_backend_model():
    assert ca._model_is_reroutable("coder") is True
    assert ca._model_is_reroutable("default") is True
    assert ca._model_is_reroutable("local_mlx:mlx-community/Qwen3-30B-A3B-4bit") is False


def test_choose_model_preserves_default_alias():
    task = {"coding_model": "default"}

    assert ca._choose_model(task, None) == "default"
    assert ca._choose_model(task, "default") == "default"
    assert ca._choose_model(task, "local_vllm:custom") == "local_vllm:custom"


@pytest.mark.asyncio
async def test_gateway_recovery_resumes_interrupted_run_with_persisted_horizon(monkeypatch):
    task = {
        "id": "code_resume",
        "agent_status": "interrupted",
        "coding_model": "coder",
        "agent_auto_commit": True,
        "agent_max_cycles": 1000,
        "agent_max_runtime_sec": 21_600,
        "agent_context_reset_cycles": 0,
    }
    started = []

    monkeypatch.setattr(ca.S, "CODING_AGENT_AUTO_RESUME_INTERRUPTED", True, raising=False)
    monkeypatch.setattr(ca.cw, "load_task", lambda _task_id: dict(task))
    monkeypatch.setattr(ca, "_git_token_for_task_owner", lambda _task: "configured-token")

    async def _start(task_id, **kwargs):
        started.append((task_id, kwargs))
        return {}

    monkeypatch.setattr(ca, "start_agent_run", _start)
    result = await ca.resume_interrupted_agent_runs(["code_resume"])

    assert result == {"ok": True, "resumed": 1, "tasks": ["code_resume"], "failures": {}}
    assert started[0][0] == "code_resume"
    assert started[0][1]["git_token_value"] == "configured-token"
    assert started[0][1]["actor"] == "gateway-recovery"
    assert started[0][1]["max_cycles"] == 1000
    assert started[0][1]["max_runtime_sec"] == 21_600


def test_validation_command_classifier_recognizes_common_checks():
    assert ca._is_validation_command(["pytest", "services/gateway/tests/test_app.py"]) is True
    assert ca._is_validation_command(["ruff", "check", "services/gateway/app"]) is True
    assert ca._is_validation_command(["python", "-m", "py_compile", "app.py"]) is True
    assert ca._is_validation_command(["python3", "-m", "pytest", "tests/test_app.py"]) is True
    assert ca._is_validation_command(["node", "--check", "static/coding.js"]) is True
    assert ca._is_validation_command(["npm", "run", "typecheck"]) is True
    assert ca._is_validation_command(["uv", "run", "python", "-m", "pytest"]) is True
    assert ca._is_validation_command(["git", "diff", "--check"]) is True


def test_validation_command_classifier_ignores_inspection_commands():
    assert ca._is_validation_command(["rg", "SomeSymbol", "services"]) is False
    assert ca._is_validation_command(["python", "scripts/print_status.py"]) is False


def test_validation_missing_tool_detection_does_not_treat_absent_pytest_as_test_failure():
    assert ca._validation_command_failed_due_to_missing_tool({"ok": False, "stderr": "command not found: pytest"}) is True
    assert ca._validation_command_failed_due_to_missing_tool({"ok": False, "stderr": "No module named pytest"}) is True
    assert ca._validation_command_failed_due_to_missing_tool({"ok": False, "stderr": "FAILED tests/test_app.py::test_bug"}) is False


def test_finish_gate_rejects_placeholder_diff_content():
    task = {
        "prompt": "Fix the broken Scheduled Tasks edit button.",
        "agent_run_prompt": "",
    }

    feedback = ca._finish_gate_feedback(
        task=task,
        finish_success=True,
        workspace_modified=True,
        diff_reviewed_after_edit=True,
        validation_run_after_edit=True,
        validation_ok_after_edit=True,
        diff_result_after_edit={
            "diff": {
                "stdout": "+++ b/services/gateway/app/static/tasks.js\n+function editSelectedTask() { /* Add logic to edit task */ }\n"
            }
        },
        validation_argv_after_edit=["node", "--check", "services/gateway/app/static/tasks.js"],
    )

    assert "placeholder or stub text" in feedback


def test_finish_gate_rejects_invented_package_manifest_for_npm_validation():
    task = {
        "prompt": "Scheduled tasks in the Scheduled Tasks UI have an Edit button that does nothing. Fix it so it works!",
        "agent_run_prompt": "",
    }

    feedback = ca._finish_gate_feedback(
        task=task,
        finish_success=True,
        workspace_modified=True,
        diff_reviewed_after_edit=True,
        validation_run_after_edit=True,
        validation_ok_after_edit=True,
        diff_result_after_edit={
            "changes": {
                "files": [
                    {"path": "services/gateway/package.json", "status": "A"},
                    {"path": "services/gateway/app/static/tasks.js", "status": "M"},
                ]
            },
            "diff": {"stdout": "+++ b/services/gateway/package.json\n+{\"name\":\"gateway\",\"scripts\":{\"test\":\"vitest\"}}\n"},
        },
        validation_argv_after_edit=["npm", "test"],
    )

    assert "introduced services/gateway/package.json only to support npm-based validation" in feedback


@pytest.mark.asyncio
async def test_request_pause_recovers_stale_active_state_without_runner(monkeypatch):
    stored = {
        "schema": "nexus_coding_task.v1",
        "id": "code_123",
        "status": "ready",
        "agent_status": "pausing",
        "agent_stop_requested": True,
        "agent_pause_requested": True,
        "agent_events": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    }

    def load_task(task_id: str):
        assert task_id == "code_123"
        return dict(stored)

    def save_task(task):
        stored.clear()
        stored.update(task)
        return task

    monkeypatch.setattr(ca, "_RUNNING", {})
    monkeypatch.setattr(ca.cw, "load_task", load_task)
    monkeypatch.setattr(ca.cw, "save_task", save_task)

    result = await ca.request_pause("code_123")

    assert stored["agent_status"] == "paused"
    assert stored["agent_stop_requested"] is False
    assert stored["agent_pause_requested"] is False
    assert result["agent"]["status"] == "paused"
    assert any(item.get("type") == "stale_agent_recovered" for item in stored["agent_events"])


def test_finish_gate_blocks_success_after_edits_without_validation_or_diff():
    feedback = ca._finish_gate_feedback(
        task={"prompt": "Fix the broken route.", "agent_run_prompt": ""},
        finish_success=True,
        workspace_modified=True,
        diff_reviewed_after_edit=False,
        validation_run_after_edit=False,
        validation_ok_after_edit=None,
    )

    assert "run a targeted validation command" in feedback
    assert "coding_git_diff" in feedback


def test_finish_gate_blocks_success_after_failed_validation():
    feedback = ca._finish_gate_feedback(
        task={"prompt": "Fix the broken route.", "agent_run_prompt": ""},
        finish_success=True,
        workspace_modified=True,
        diff_reviewed_after_edit=True,
        validation_run_after_edit=True,
        validation_ok_after_edit=False,
        validation_failed_after_edit=True,
    )

    assert "validation command failed" in feedback
    assert "success=false" in feedback


def test_finish_gate_does_not_hide_failed_validation_with_later_weak_check():
    feedback = ca._finish_gate_feedback(
        task={"prompt": "Fix the broken route.", "agent_run_prompt": ""},
        finish_success=True,
        workspace_modified=True,
        diff_reviewed_after_edit=True,
        validation_run_after_edit=True,
        validation_ok_after_edit=True,
        validation_failed_after_edit=True,
    )

    assert "validation command failed" in feedback


def test_finish_gate_allows_success_after_validation_and_diff():
    feedback = ca._finish_gate_feedback(
        task={"prompt": "Fix the broken route.", "agent_run_prompt": ""},
        finish_success=True,
        workspace_modified=True,
        diff_reviewed_after_edit=True,
        validation_run_after_edit=True,
        validation_ok_after_edit=True,
    )

    assert feedback == ""


def test_tool_result_modified_workspace_tracks_real_edit_tools():
    assert ca._tool_result_modified_workspace("coding_write_file", {}, {"ok": True, "bytes": 10}) is True
    assert ca._tool_result_modified_workspace("coding_replace_text", {}, {"ok": True, "replacements": 1}) is True
    assert ca._tool_result_modified_workspace("coding_apply_patch", {}, {"ok": True, "apply": {"ok": True}}) is True
    assert ca._tool_result_modified_workspace("coding_apply_patch", {"check_only": True}, {"ok": True, "check_only": True}) is False
    assert ca._tool_result_modified_workspace("coding_replace_text", {}, {"ok": False, "replacements": 0}) is False


def test_backend_supports_tool_calling_prefers_payload_policy(monkeypatch):
    class FakeBackend:
        def __init__(self, provider: str, policy):
            self.provider = provider
            self.payload_policy = policy

    class FakeRegistry:
        def get_backend(self, backend_name: str):
            return {
                "local_mlx": FakeBackend("mlx", {"supports_tool_calling": True}),
                "local_vllm": FakeBackend("vllm", {"supports_tool_calling": False}),
                "legacy_mlx": FakeBackend("mlx", {}),
            }.get(backend_name)

    monkeypatch.setattr(ca, "get_registry", lambda: FakeRegistry())

    assert ca._backend_supports_tool_calling("local_mlx") is True
    assert ca._backend_supports_tool_calling("local_vllm") is False
    assert ca._backend_supports_tool_calling("legacy_mlx") is True


def test_tools_true_alias_allows_preferred_vllm_coding_route(monkeypatch):
    class FakeBackend:
        def __init__(self, provider: str, policy):
            self.provider = provider
            self.payload_policy = policy

    class FakeRegistry:
        def get_backend(self, backend_name: str):
            return {
                "local_vllm": FakeBackend("vllm", {"supports_tool_calling": False}),
            }.get(backend_name)

        def resolve_backend_class(self, backend_name: str):
            return backend_name

    monkeypatch.setattr(ca, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(
        ca,
        "get_aliases",
        lambda: {
            "default": SimpleNamespace(backend="local_vllm", upstream_model="qwen-default", tools=True),
            "fast": SimpleNamespace(backend="local_vllm", upstream_model="qwen-fast", tools=False),
        },
    )
    monkeypatch.setattr(ca, "llm_backends", lambda: [])

    assert ca._preferred_route_supports_coding_tools("default", "local_vllm") is True
    assert ca._preferred_route_supports_coding_tools("fast", "local_vllm") is False
    assert ca._coding_candidate_routes("default", "local_vllm", "qwen-default") == [("local_vllm", "qwen-default")]


def test_rank_coding_backend_candidates_keeps_ready_alias_backend_first(monkeypatch):
    class FakeBackend:
        def __init__(self, base_url: str, provider: str, policy):
            self.base_url = base_url
            self.provider = provider
            self.payload_policy = policy

        def supports(self, route_kind: str) -> bool:
            return route_kind == "chat"

        def get_limit(self, route_kind: str) -> int:
            return 4

    class FakeRegistry:
        def __init__(self):
            self.backends = {
                "local_mlx": FakeBackend("http://ai2:10240/v1", "mlx", {"supports_tool_calling": True}),
                "local_vllm": FakeBackend("http://ada2:8000/v1", "vllm", {"supports_tool_calling": False}),
            }

        def get_backend(self, backend_name: str):
            return self.backends.get(backend_name)

        def resolve_backend_class(self, backend_name: str):
            return backend_name

    class FakeChecker:
        def get_status(self, backend_name: str):
            return SimpleNamespace(error="")

        def is_ready(self, backend_name: str) -> bool:
            return True

    class FakeAdmission:
        def get_stats(self):
            return {
                "local_mlx.chat": {"limit": 4, "available": 4, "inflight": 0},
                "local_vllm.chat": {"limit": 4, "available": 1, "inflight": 3},
            }

    monkeypatch.setattr(ca, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(ca, "get_health_checker", lambda: FakeChecker())
    monkeypatch.setattr(ca, "get_admission_controller", lambda: FakeAdmission())
    monkeypatch.setattr(ca, "llm_backends", lambda: [("local_mlx", object()), ("local_vllm", object())])
    monkeypatch.setattr(ca, "backend_hostname", lambda backend_name, **kwargs: {"local_mlx": "ai2", "local_vllm": "ada2"}[backend_name])
    monkeypatch.setattr(ca, "default_model_for_backend", lambda backend_name, cfg: f"model-for-{backend_name}")
    monkeypatch.setattr(
        ca,
        "get_aliases",
        lambda: {"default": SimpleNamespace(backend="local_vllm", upstream_model="qwen-default", tools=True)},
    )

    ranked = ca._rank_coding_backend_candidates("default", "local_vllm", "qwen-default")

    assert [item["backend"] for item in ranked[:2]] == ["local_vllm", "local_mlx"]


def test_rank_coding_backend_candidates_prefers_less_loaded_ready_host(monkeypatch):
    class FakeBackend:
        def __init__(self, base_url: str, limit: int, provider: str, policy=None):
            self.base_url = base_url
            self._limit = limit
            self.provider = provider
            self.payload_policy = policy if isinstance(policy, dict) else {}

        def supports(self, route_kind: str) -> bool:
            return route_kind == "chat"

        def get_limit(self, route_kind: str) -> int:
            return self._limit

    class FakeRegistry:
        def __init__(self):
            self.backends = {
                "local_mlx": FakeBackend("http://ai2:10240/v1", 2, "mlx", {"supports_tool_calling": True}),
                "local_vllm": FakeBackend("http://ada2:8000/v1", 4, "vllm", {"supports_tool_calling": False}),
                "local_vllm_fast": FakeBackend("http://stackrot:8001/v1", 4, "vllm", {"supports_tool_calling": False}),
            }

        def get_backend(self, backend_name: str):
            return self.backends.get(backend_name)

    class FakeChecker:
        def get_status(self, backend_name: str):
            return SimpleNamespace(error="")

        def is_ready(self, backend_name: str) -> bool:
            return backend_name != "local_mlx"

    class FakeAdmission:
        def get_stats(self):
            return {
                "local_mlx.chat": {"limit": 2, "available": 0, "inflight": 2},
                "local_vllm.chat": {"limit": 4, "available": 3, "inflight": 1},
                "local_vllm_fast.chat": {"limit": 4, "available": 4, "inflight": 0},
            }

    monkeypatch.setattr(ca, "get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(ca, "get_health_checker", lambda: FakeChecker())
    monkeypatch.setattr(ca, "get_admission_controller", lambda: FakeAdmission())
    monkeypatch.setattr(ca, "llm_backends", lambda: [("local_mlx", object()), ("local_vllm", object()), ("local_vllm_fast", object())])
    monkeypatch.setattr(ca, "backend_hostname", lambda backend_name, **kwargs: {"local_mlx": "ai2", "local_vllm": "ada2", "local_vllm_fast": "stackrot"}[backend_name])
    monkeypatch.setattr(ca, "default_model_for_backend", lambda backend_name, cfg: f"model-for-{backend_name}")

    ranked = ca._rank_coding_backend_candidates("coder", "local_mlx", "model-for-local_mlx")

    assert [item["backend"] for item in ranked] == ["local_mlx"]
    assert ranked[0]["ready"] is False


@pytest.mark.asyncio
async def test_start_agent_run_defers_inactive_pinned_huge_model(monkeypatch):
    task = {
        "id": "code_abcdef123456",
        "status": "ready",
        "agent_status": "idle",
        "coding_model": "model-b",
        "prompt": "Fix the bug.",
        "agent_events": [],
        "guidance_messages": [{"ts": 1.0, "role": "user", "content": "Keep this guidance."}],
    }
    store = {"task": dict(task)}

    def _load_task(task_id):
        assert task_id == "code_abcdef123456"
        return dict(store["task"])

    def _save_task(next_task):
        store["task"] = dict(next_task)
        return next_task

    monkeypatch.setattr(ca.cw, "_ensure_enabled", lambda: None)
    monkeypatch.setattr(ca.cw, "load_task", _load_task)
    monkeypatch.setattr(ca.cw, "save_task", _save_task)
    monkeypatch.setattr(
        ca.coding_model_policy,
        "describe_workspace_model",
        lambda model: {
            "selected_model": model,
            "resolved_model": model,
            "run_policy": "idle_only",
            "warning": "This workspace will only run during idle periods.",
            "active_huge_model": "model-a",
            "recommended_model": "coder",
        },
    )

    result = await ca.start_agent_run(
        "code_abcdef123456",
        coding_model="model-b",
        actor="test",
        prompt="Also update the tests.",
        max_cycles=120,
        max_runtime_sec=7_200,
        context_reset_cycles=10,
    )

    assert result["agent"]["status"] == "idle_waiting"
    assert result["agent"]["summary"] == "This workspace will only run during idle periods."
    assert store["task"]["agent_status"] == "idle_waiting"
    assert store["task"]["agent_events"][-1]["type"] == "idle_deferred"
    assert store["task"]["agent_runs"][-1]["status"] == "idle_waiting"
    assert store["task"]["agent_runs"][-1]["max_cycles"] == 120
    assert store["task"]["agent_runs"][-1]["max_runtime_sec"] == 7_200
    assert store["task"]["agent_runs"][-1]["context_reset_cycles"] == 10
    assert [item["content"] for item in store["task"]["guidance_messages"]] == [
        "Keep this guidance.",
        "Also update the tests.",
    ]
    assert ca._active_runner("code_abcdef123456") is None
