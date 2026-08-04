from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[3]


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_regex(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected one regex match, found {count}")
    path.write_text(updated, encoding="utf-8")


forced_action = dedent(r'''
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Sequence

from app import coding_stagnation_resilience as resilience


SCHEMA = "nexus_coding_forced_action.v1"
_ALLOWED_TOOLS = {
    "coding_write_file",
    "coding_replace_text",
    "coding_apply_patch",
    "coding_run_command",
    "coding_git_diff",
    "coding_finish",
}


def active_state(task: Mapping[str, Any]) -> Dict[str, Any]:
    raw = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    if str(raw.get("schema") or "") != SCHEMA or str(raw.get("status") or "") != "active":
        return {}
    if str(raw.get("state_key") or "") != resilience.durable_state_key(task):
        return {}
    return dict(raw)


def activate(
    task: Mapping[str, Any],
    *,
    state_key: str,
    run_id: str,
    cycle: int,
    stage: str,
    required_action: str,
) -> Dict[str, Any]:
    previous = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    same_state = str(previous.get("state_key") or "") == state_key
    previous_run = str(previous.get("run_id") or "")
    activation_count = int(previous.get("activation_count") or 0) + (0 if same_state and previous_run == run_id else 1)
    resume_count = int(previous.get("resume_count") or 0)
    if same_state and previous_run and previous_run != run_id:
        resume_count += 1
    now = time.time()
    return {
        "schema": SCHEMA,
        "status": "active",
        "state_key": state_key,
        "run_id": run_id,
        "cycle": int(cycle or 0),
        "stage": str(stage or "interrupt"),
        "required_action": str(required_action or "Take one edit, targeted validation, diff review, or terminal action.").strip(),
        "allowed_tools": sorted(_ALLOWED_TOOLS),
        "activation_count": max(1, activation_count),
        "resume_count": resume_count,
        "rejection_limit": 2,
        "activated_at": float(previous.get("activated_at") or now) if same_state else now,
        "updated_at": now,
    }


def retire_if_state_changed(task: Dict[str, Any], *, state_key: str) -> bool:
    current = task.get("agent_forced_action") if isinstance(task.get("agent_forced_action"), dict) else {}
    if str(current.get("status") or "") != "active":
        return False
    if str(current.get("state_key") or "") == state_key:
        return False
    history = [item for item in (task.get("agent_forced_action_history") or []) if isinstance(item, dict)]
    retired = dict(current)
    retired.update({"status": "superseded", "superseded_by_state_key": state_key, "retired_at": time.time()})
    history.append(retired)
    task["agent_forced_action_history"] = history[-16:]
    task["agent_forced_action"] = retired
    return True


def allowed_tool_names(task: Mapping[str, Any]) -> set[str]:
    return set(_ALLOWED_TOOLS) if active_state(task) else set()


def evaluate_tool_call(
    task: Mapping[str, Any],
    *,
    name: str,
    args: Mapping[str, Any],
    is_validation_command: Callable[[Any], bool],
) -> tuple[bool, Dict[str, Any]]:
    state = active_state(task)
    if not state:
        return True, {}
    tool_name = str(name or "").strip()
    allowed = tool_name in _ALLOWED_TOOLS
    if tool_name == "coding_run_command":
        allowed = bool(is_validation_command(args.get("argv")))
    if allowed:
        return True, {}
    required_action = str(state.get("required_action") or "").strip()
    message = (
        f"Forced-action mode rejected {tool_name or '(missing tool name)'}. "
        "Inspection and arbitrary shell commands are disabled for this unchanged durable state. "
        f"Required action: {required_action}"
    )
    return False, {
        "ok": False,
        "error": "forced_action_tool_rejected",
        "message": message,
        "required_action": required_action,
        "allowed_tools": sorted(_ALLOWED_TOOLS),
        "state_key": state.get("state_key"),
        "stage": state.get("stage"),
    }


def prompt_context(task: Mapping[str, Any]) -> str:
    state = active_state(task)
    if not state:
        return ""
    return (
        "Controller forced-action mode is ACTIVE for the unchanged durable state. "
        "Inspection tools, repository orientation, plan churn, and arbitrary shell commands are unavailable. "
        f"Required action: {state.get('required_action') or ''} "
        "Use exactly one of: a focused edit, a recognized validation command, coding_git_diff, or coding_finish."
    )


def filter_tool_specs(specs: Sequence[Any], task: Mapping[str, Any]) -> list[Any]:
    allowed = allowed_tool_names(task)
    if not allowed:
        return list(specs)
    out = []
    for spec in specs:
        try:
            name = str(spec.function.name)
        except Exception:
            continue
        if name in allowed:
            out.append(spec)
    return out
''').lstrip()
(ROOT / "services/gateway/app/coding_forced_action.py").write_text(forced_action, encoding="utf-8")

# Extract concrete model commitments and replace stale generic directives.
resilience_path = ROOT / "services/gateway/app/coding_stagnation_resilience.py"
replace_once(
    resilience_path,
    "def active_plan_summary(task: Mapping[str, Any]) -> str:\n",
    dedent(r'''
_COMMITMENT_PATTERN = re.compile(
    r"(?:\\bI(?:'ll| will| need to| should)\\b|\\b(?:now )?let me\\b|\\bnext(?:,? I(?:'ll| will))?\\b)"
    r"(?:(?![.!?\\n]).){0,180}?\\b"
    r"(add|write|implement|fix|edit|update|remove|create|patch|run|finish)\\b\\s+"
    r"([^.!?\\n]{3,260})",
    re.IGNORECASE,
)
_GENERIC_ACTION_PREFIXES = (
    "take one bounded action",
    "stop broad inspection",
    "convert the review finding",
    "fix one concrete validation failure",
    "stop revising notes",
    "call one workspace tool",
)


def extract_concrete_commitment(events: Sequence[Mapping[str, Any]]) -> str:
    for event in reversed(events):
        if str(event.get("type") or "") != "assistant":
            continue
        text = " ".join(str(event.get("content") or "").split())
        if not text:
            continue
        matches = list(_COMMITMENT_PATTERN.finditer(text))
        if not matches:
            continue
        match = matches[-1]
        verb = match.group(1).lower()
        target = match.group(2).strip(" `:;- ")
        target = re.split(r"\\b(?:before|after|then)\\b", target, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,;-")
        if len(target) < 3:
            continue
        return clip(f"{verb.capitalize()} {target}.", 700)
    return ""


def generic_next_action(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
    return not normalized or any(normalized.startswith(prefix) for prefix in _GENERIC_ACTION_PREFIXES)


def active_plan_summary(task: Mapping[str, Any]) -> str:
''').lstrip(),
    "insert commitment extraction",
)
replace_once(
    resilience_path,
    dedent('''
    unresolved_default, next_default = defaults.get(classification, defaults["stagnant_execution"])
    previous_directives = previous if str(previous.get("state_key") or "") == state_key else {}
    unresolved = clip(previous_directives.get("unresolved_question") or unresolved_default, 700)
    next_action = clip(previous_directives.get("next_action") or next_default, 700)
    blocker = clip(previous_directives.get("blocker"), 700)
    content = {
        "state_key": state_key, "findings": findings, "inspected_targets": inspected,
        "unresolved_question": unresolved, "next_action": next_action,
        "blocker": blocker, "classification": classification,
    }
'''),
    dedent('''
    unresolved_default, next_default = defaults.get(classification, defaults["stagnant_execution"])
    previous_directives = previous if str(previous.get("state_key") or "") == state_key else {}
    commitment = extract_concrete_commitment(events)
    previous_action = clip(previous_directives.get("next_action"), 700)
    next_action = commitment if commitment and generic_next_action(previous_action) else (previous_action or next_default)
    action_source = "assistant_commitment" if commitment and next_action == commitment else "controller_default"
    unresolved = clip(previous_directives.get("unresolved_question") or unresolved_default, 700)
    if action_source == "assistant_commitment" and generic_next_action(previous_directives.get("unresolved_question")):
        unresolved = clip(f"What remains before executing this commitment: {commitment}", 700)
    next_action = clip(next_action, 700)
    blocker = clip(previous_directives.get("blocker"), 700)
    content = {
        "state_key": state_key, "findings": findings, "inspected_targets": inspected,
        "unresolved_question": unresolved, "next_action": next_action,
        "required_action_source": action_source,
        "blocker": blocker, "classification": classification,
    }
'''),
    "derive concrete required action",
)
replace_once(
    resilience_path,
    '        "next_action": next_action,\n        "blocker": blocker,\n',
    '        "next_action": next_action,\n        "required_action_source": action_source,\n        "blocker": blocker,\n',
    "persist required action source",
)

# Persist forced action at interrupt/recovery/continuation and stop refreshing continuation credits.
semantic_path = ROOT / "services/gateway/app/coding_semantic_memory.py"
replace_once(
    semantic_path,
    "from app import coding_stagnation_resilience as resilience\n",
    "from app import coding_stagnation_resilience as resilience\nfrom app import coding_forced_action as forced_action\n",
    "import forced action",
)
replace_once(
    semantic_path,
    '        task["agent_context_manifest"] = dict(checkpoint.get("context_manifest") or {})\n        persisted["value"] = True\n',
    '        task["agent_context_manifest"] = dict(checkpoint.get("context_manifest") or {})\n        forced_action.retire_if_state_changed(task, state_key=state_key)\n        persisted["value"] = True\n',
    "retire stale forced state",
)
replace_once(
    semantic_path,
    '        task["agent_context_manifest"] = dict(checkpoint.get("context_manifest") or {})\n        task["agent_investigation_checkpoint"] = dict(checkpoint)\n',
    '        task["agent_context_manifest"] = dict(checkpoint.get("context_manifest") or {})\n        forced_action.retire_if_state_changed(task, state_key=state_key)\n        if kind in {"interrupt", "recovery", "continuation"}:\n            task["agent_forced_action"] = forced_action.activate(\n                task,\n                state_key=state_key,\n                run_id=run_id,\n                cycle=_as_int(checkpoint.get("cycle")),\n                stage=kind,\n                required_action=str((checkpoint.get("working_memory") or {}).get("next_action") or checkpoint.get("next_action") or ""),\n            )\n        task["agent_investigation_checkpoint"] = dict(checkpoint)\n',
    "activate forced action",
)
replace_once(
    semantic_path,
    '        recovery_kind = "continuation" if kind == "continuation" else "checkpoint"\n',
    '        recovery_kind = "checkpoint"\n',
    "remove continuation recovery kind",
)
replace_once(
    semantic_path,
    '        if kind in {"assist", "continuation"} and recovery_id not in recovery_history:\n',
    '        if kind == "assist" and recovery_id not in recovery_history:\n',
    "limit recovery credit to assist",
)
replace_once(
    semantic_path,
    dedent('''
    if _repair_legacy_consumed_continuation(task_id, task):
        task = cw.load_task(task_id)
    if _consume_recovery_lease(task_id, task):
'''),
    dedent('''
    # Unchanged resumes retain escalation. Legacy continuation credits are not
    # revived because doing so recreates the full inspection window.
    if _consume_recovery_lease(task_id, task):
'''),
    "do not repair continuation reset",
)

# Infer review-only mission completion policy and stop universal commit overrides.
workspace_path = ROOT / "services/gateway/app/coding_workspace.py"
replace_once(
    workspace_path,
    "def normalize_coding_mission(task: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:\n",
    dedent(r'''
_DIRECT_CHANGE_RE = re.compile(
    r"\\b(fix|repair|resolve|implement|edit|modify|patch|add|remove|create|rewrite|change|update)\\b",
    re.IGNORECASE,
)
_REVIEW_GOAL_MARKERS = (
    "review this workspace",
    "review scope",
    "review only",
    "audit",
    "concrete findings",
    "behavioral regressions",
    "risky assumptions",
    "missing tests",
    "inspect relevant diffs",
)


def goal_expects_file_changes(goal: str) -> bool:
    text = " ".join(str(goal or "").strip().lower().split())
    if not text:
        return True
    review_goal = any(marker in text for marker in _REVIEW_GOAL_MARKERS)
    return bool(_DIRECT_CHANGE_RE.search(text)) or not review_goal


def normalize_coding_mission(task: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
''').lstrip(),
    "insert review mission classifier",
)
replace_once(
    workspace_path,
    '    max_no_progress_cycles = int(budget.get("max_no_progress_cycles") or 8)\n    return {\n',
    '    max_no_progress_cycles = int(budget.get("max_no_progress_cycles") or 8)\n    expects_file_changes = goal_expects_file_changes(prompt)\n\n    def completion_bool(name: str, default: bool) -> bool:\n        return bool(completion[name]) if name in completion else default\n\n    return {\n',
    "derive mission completion defaults",
)
replace_once(
    workspace_path,
    dedent('''
        "completion_policy": {
            "require_file_changes": bool(completion.get("require_file_changes", True)),
            "require_validation_after_edit": bool(completion.get("require_validation_after_edit", True)),
            "require_diff_review_after_edit": bool(completion.get("require_diff_review_after_edit", True)),
            "require_commit_on_success": bool(completion.get("require_commit_on_success", True)),
            "commit_policy": str(completion.get("commit_policy") or "always_on_success"),
        },
'''),
    dedent('''
        "completion_policy": {
            "require_file_changes": completion_bool("require_file_changes", expects_file_changes),
            "require_validation_after_edit": completion_bool("require_validation_after_edit", True),
            "require_diff_review_after_edit": completion_bool("require_diff_review_after_edit", True),
            "require_commit_on_success": completion_bool("require_commit_on_success", expects_file_changes),
            "commit_policy": str(completion.get("commit_policy") or "always_on_success"),
        },
'''),
    "review mission completion policy",
)
replace_once(
    workspace_path,
    '        "completion_policy": {\n            "require_commit_on_success": True,\n            "commit_policy": str(commit_policy or "always_on_success"),\n        },\n',
    '        "completion_policy": {\n            "commit_policy": str(commit_policy or "always_on_success"),\n        },\n',
    "remove universal commit override",
)

# Filter advertised tools, reject disallowed calls, reroute or pause noncompliant models,
# and allow clean review completion without a new commit.
agent_path = ROOT / "services/gateway/app/coding_agent.py"
replace_once(
    agent_path,
    "from app import coding_semantic_memory\n",
    "from app import coding_semantic_memory\nfrom app import coding_forced_action as forced_action\n",
    "import forced action into agent",
)
replace_once(
    agent_path,
    dedent('''
    completion = contract["completion_policy"]
    now = time.time()
'''),
    dedent('''
    completion = contract["completion_policy"]
    expects_workspace_edits = _request_expects_workspace_edits(task)
    now = time.time()
'''),
    "finalizer edit expectation",
)
replace_once(
    agent_path,
    dedent('''
        if completion.get("require_file_changes", True) and int(base_counts.get("total") or 0) <= 0:
            raise RuntimeError("successful run has no meaningful delta versus the base branch")
        if completion.get("require_validation_after_edit", True) and not bool((snapshot.get("validation") or {}).get("validation_after_latest_edit")):
            raise RuntimeError("successful run lacks validation after the latest edit")
        if completion.get("require_diff_review_after_edit", True) and not bool((snapshot.get("diff_review") or {}).get("diff_reviewed_after_latest_edit")):
            raise RuntimeError("successful run lacks diff review after the latest edit")
'''),
    dedent('''
        actual_delta = int(base_counts.get("total") or 0) > 0 or has_uncommitted
        require_file_changes = bool(completion.get("require_file_changes", True)) and expects_workspace_edits
        require_commit_on_success = bool(completion.get("require_commit_on_success", True)) and (expects_workspace_edits or actual_delta)
        if require_file_changes and not actual_delta:
            raise RuntimeError("successful run has no meaningful delta versus the base branch")
        if actual_delta and completion.get("require_validation_after_edit", True) and not bool((snapshot.get("validation") or {}).get("validation_after_latest_edit")):
            raise RuntimeError("successful run lacks validation after the latest edit")
        if actual_delta and completion.get("require_diff_review_after_edit", True) and not bool((snapshot.get("diff_review") or {}).get("diff_reviewed_after_latest_edit")):
            raise RuntimeError("successful run lacks diff review after the latest edit")
'''),
    "conditional review finalization gates",
)
agent_text = agent_path.read_text(encoding="utf-8")
agent_text = agent_text.replace('if completion.get("require_file_changes", True) and not candidate:', 'if require_file_changes and not candidate:')
agent_text = agent_text.replace('if completion.get("require_file_changes", True) and start_head and candidate == start_head and not checkpoint_for_run:', 'if require_file_changes and start_head and candidate == start_head and not checkpoint_for_run:')
agent_text = agent_text.replace('if completion.get("require_commit_on_success", True) and not result["final_commit"]:', 'if require_commit_on_success and not result["final_commit"]:')
agent_path.write_text(agent_text, encoding="utf-8")

replace_once(
    agent_path,
    dedent('''
def _cancelled_run_status(task: Dict[str, Any]) -> str:
'''),
    dedent('''
def _tool_specs_for_task(task: Dict[str, Any]) -> List[ToolSpec]:
    return forced_action.filter_tool_specs(_tool_specs(), task)


def _forced_action_context(task: Dict[str, Any]) -> str:
    return forced_action.prompt_context(task)


def _cancelled_run_status(task: Dict[str, Any]) -> str:
'''),
    "insert forced tool helpers",
)
replace_once(
    agent_path,
    "def coding_tool_manifest() -> Dict[str, Any]:\n    tools = [spec.model_dump(exclude_none=True) for spec in _tool_specs()]\n",
    "def coding_tool_manifest(task: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:\n    specs = _tool_specs_for_task(task) if isinstance(task, dict) else _tool_specs()\n    tools = [spec.model_dump(exclude_none=True) for spec in specs]\n",
    "filter tool manifest",
)
replace_once(
    agent_path,
    "def _text_tool_call_guidance() -> str:\n    tools = []\n    for spec in _tool_specs():\n",
    "def _text_tool_call_guidance(task: Optional[Dict[str, Any]] = None) -> str:\n    tools = []\n    specs = _tool_specs_for_task(task) if isinstance(task, dict) else _tool_specs()\n    for spec in specs:\n",
    "filter text tool guidance",
)
replace_once(
    agent_path,
    '            f"{_text_tool_call_guidance()} "\n',
    '            f"{_text_tool_call_guidance(task)} "\n',
    "pass task to text guidance",
)
replace_once(
    agent_path,
    dedent('''
    if _request_expects_workspace_edits(task):
        edit_expectation = (
            "This request is fix-oriented. After you identify the concrete root cause, make the smallest viable workspace edit "
            "that addresses it, run a targeted validation step, inspect the resulting diff, and only then finish. "
            "Do not stop at diagnosis alone when a focused fix is available. "
        )
    if text_tool_mode:
'''),
    dedent('''
    if _request_expects_workspace_edits(task):
        edit_expectation = (
            "This request is fix-oriented. After you identify the concrete root cause, make the smallest viable workspace edit "
            "that addresses it, run a targeted validation step, inspect the resulting diff, and only then finish. "
            "Do not stop at diagnosis alone when a focused fix is available. "
        )
    forced_context = _forced_action_context(task)
    if forced_context:
        edit_expectation += forced_context + " "
    if text_tool_mode:
'''),
    "render forced action in system prompt",
)
replace_once(
    agent_path,
    "        tools = _tool_specs()\n        no_tool_cycles = 0\n        semantic_reroutes = 0\n",
    "        tools = _tool_specs_for_task(task)\n        no_tool_cycles = 0\n        semantic_reroutes = 0\n        forced_action_rejections = 0\n",
    "initialize forced action counters",
)
replace_once(
    agent_path,
    "            context_chars = _messages_char_count(messages)\n            request_text_tool_mode = not _backend_supports_tool_calling(backend)\n",
    "            latest_policy_task = await asyncio.to_thread(cw.load_task, task_id)\n            tools = _tool_specs_for_task(latest_policy_task)\n            context_chars = _messages_char_count(messages)\n            request_text_tool_mode = not _backend_supports_tool_calling(backend)\n",
    "refresh forced tool policy each cycle",
)
replace_once(
    agent_path,
    dedent('''
                try:
                    result = await asyncio.to_thread(_run_tool, task_id, name, args, git_token_value=git_token_value)
                except HTTPException as exc:
                    result = {"ok": False, "error": exc.detail}
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

                state_read = _state_read_signature(name, args)
'''),
    dedent('''
                policy_task = await asyncio.to_thread(cw.load_task, task_id)
                allowed_call, rejection = forced_action.evaluate_tool_call(
                    policy_task,
                    name=name,
                    args=args,
                    is_validation_command=_is_validation_command,
                )
                rejected_by_forced_action = not allowed_call
                if rejected_by_forced_action:
                    result = rejection
                    forced_action_rejections += 1
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "forced_action_tool_rejected",
                            "cycle": cycle,
                            "name": name,
                            "count": forced_action_rejections,
                            "state_key": rejection.get("state_key"),
                            "required_action": rejection.get("required_action"),
                            "summary": rejection.get("message"),
                        },
                    )
                else:
                    try:
                        result = await asyncio.to_thread(_run_tool, task_id, name, args, git_token_value=git_token_value)
                    except HTTPException as exc:
                        result = {"ok": False, "error": exc.detail}
                    except Exception as exc:
                        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

                state_read = "" if rejected_by_forced_action else _state_read_signature(name, args)
'''),
    "enforce forced action at execution",
)
replace_once(
    agent_path,
    dedent('''
                if name == "coding_finish" and bool(result.get("ok")):
                    finish_summary = str(result.get("summary") or args.get("summary") or "").strip()
                    finish_success = bool(result.get("success", args.get("success", True)))
                    finish_called = True
                    stop_after_tools = True
                    break

            if _checkpoint_enabled():
'''),
    dedent('''
                if rejected_by_forced_action and forced_action_rejections >= int((forced_action.active_state(policy_task) or {}).get("rejection_limit") or 2):
                    fallback = None
                    if not user_llm.is_user_model_id(model) and semantic_reroutes < _max_semantic_reroutes():
                        fallback = _semantic_reroute_candidate(
                            model,
                            backend,
                            upstream_model,
                            excluded_backends=semantic_failed_backends | {backend},
                        )
                    if fallback is not None:
                        previous_backend = backend
                        previous_model = upstream_model
                        semantic_failed_backends.add(previous_backend)
                        backend = str(fallback.get("backend") or backend)
                        upstream_model = str(fallback.get("upstream_model") or upstream_model)
                        semantic_reroutes += 1
                        forced_action_rejections = 0
                        reroute_notice = (
                            "The coding backend ignored enforced forced-action tool policy. "
                            f"Rerouting from {previous_backend} to {backend}; inspection remains disabled."
                        )
                        await asyncio.to_thread(
                            _append_event,
                            task_id,
                            {
                                "type": "forced_action_reroute",
                                "cycle": cycle,
                                "previous_backend": previous_backend,
                                "previous_upstream_model": previous_model,
                                "backend": backend,
                                "upstream_model": upstream_model,
                                "summary": reroute_notice,
                            },
                        )
                        messages.append(ChatMessage(role="user", content=reroute_notice))
                        break
                    failure = (
                        "The coding model repeatedly requested inspection tools after the controller entered forced-action mode. "
                        "The run was paused immediately instead of granting another inspection window. Resume with a different model "
                        "or provide guidance that changes the required action."
                    )
                    await asyncio.to_thread(
                        _append_event,
                        task_id,
                        {
                            "type": "forced_action_noncompliance",
                            "cycle": cycle,
                            "count": forced_action_rejections,
                            "summary": failure,
                        },
                    )
                    raise _CodingAgentPaused(
                        failure,
                        reason_code="forced_action_noncompliance",
                        details={"cycle": cycle, "rejections": forced_action_rejections},
                    )

                if name == "coding_finish" and bool(result.get("ok")):
                    finish_summary = str(result.get("summary") or args.get("summary") or "").strip()
                    finish_success = bool(result.get("success", args.get("success", True)))
                    finish_called = True
                    stop_after_tools = True
                    break

            if _checkpoint_enabled():
'''),
    "reroute or pause forced noncompliance",
)

# Update old continuation expectations and the real-loop regression.
stagnation_test = ROOT / "services/gateway/tests/test_coding_stagnation_resilience.py"
replace_regex(
    stagnation_test,
    r"def test_no_progress_continuation_receives_one_state_keyed_recovery\(monkeypatch\):.*?\n\ndef test_guidance_interventions_are_scoped_to_run_but_recovery_credit_is_not",
    dedent('''
def test_no_progress_continuation_enters_forced_action_without_fresh_recovery(monkeypatch):
    task = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_controller"] = {
        "schema": resilience.SCHEMA,
        "state_key": key,
        "run_id": "run-2",
        "last_cycle": 8,
        "cycles": 8,
        "plan_revision": 1,
        "processed_event_count": len(resilience.current_run_events(task)),
        "interventions": [],
    }
    task["agent_previous_status"] = "paused"
    task["agent_previous_summary"] = "Coding run paused after 8 cycles without a durable state transition."
    task["agent_previous_stop_reason_code"] = "no_progress_limit"
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 1
    task["agent_events"].append({"type": "started", "run_id": "run-3", "ts": 6})
    _install_workspace(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    assert "agent_stagnation_recovery_lease" not in task
    assert task["agent_forced_action"]["status"] == "active"
    assert task["agent_forced_action"]["state_key"] == key
    assert task["agent_forced_action"]["stage"] == "continuation"


def test_guidance_interventions_are_scoped_to_run_but_recovery_credit_is_not''').lstrip(),
    "replace continuation recovery expectation",
)
replace_regex(
    stagnation_test,
    r"def test_legacy_consumed_continuation_repairs_controller_once\(monkeypatch\):.*?\n\ndef test_context_manifest_records_compaction_provenance",
    dedent('''
def test_legacy_consumed_continuation_is_not_revived_by_process_task(monkeypatch):
    task = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(task)
    task["agent_stagnation_controller"] = {
        "schema": resilience.SCHEMA,
        "state_key": key,
        "run_id": "run-2",
        "last_cycle": 8,
        "cycles": 12,
        "progress_stagnant_cycles": 8,
        "plan_revision": 1,
        "interventions": [],
    }
    task["agent_previous_status"] = "paused"
    task["agent_previous_stop_reason_code"] = "no_progress_limit"
    task["agent_run_id"] = "run-3"
    task["agent_cycle"] = 1
    task["agent_events"].append({"type": "started", "run_id": "run-3", "ts": 6})
    _install_workspace(monkeypatch, task)

    assert memory.process_task(task["id"]) is True
    assert task["agent_progress_state"]["stagnant_cycles"] == 8
    assert task["agent_forced_action"]["status"] == "active"
    assert "agent_stagnation_recovery_lease" not in task


def test_context_manifest_records_compaction_provenance''').lstrip(),
    "replace legacy continuation repair expectation",
)

runtime_test = ROOT / "services/gateway/tests/test_coding_runtime_guardrails.py"
replace_regex(
    runtime_test,
    r"@pytest.mark.asyncio\nasync def test_real_agent_loop_grants_one_semantic_recovery_then_pauses\(monkeypatch\) -> None:.*?\n\ndef test_noop_write_fingerprint_is_not_progress",
    dedent('''
@pytest.mark.asyncio
async def test_real_agent_loop_enforces_forced_action_against_repeated_reads(monkeypatch) -> None:
    task = {
        "id": "code_test",
        "prompt": "Review this workspace for behavioral regressions and missing tests.",
        "agent_status": "queued",
        "agent_pause_requested": False,
        "agent_stop_requested": False,
        "agent_runs": [{"run_id": "run", "status": "queued"}],
        "agent_events": [],
        "guidance_messages": [],
        "project_plan": {"revision": 0},
    }
    mission = {
        "budget_policy": {
            "max_no_progress_cycles": 8,
            "max_repeated_state_reads": 100,
            "max_repeated_same_file_reads": 100,
        },
        "context_policy": {"context_reset_chars": 64_000},
        "completion_policy": {"require_file_changes": False, "require_commit_on_success": False},
    }
    executed_reads = 0
    advertised_tool_sets = []

    def mutate_task(_task_id, mutator):
        mutator(task)
        return task

    async def backend_call(req, backend, upstream_model, **kwargs):
        names = []
        for spec in req.tools or []:
            fn = spec.get("function") if isinstance(spec, dict) else None
            if isinstance(fn, dict):
                names.append(str(fn.get("name") or ""))
        advertised_tool_sets.append(names)
        return {}, backend, upstream_model

    def run_tool(_task_id, name, args, *, git_token_value):
        nonlocal executed_reads
        executed_reads += 1
        return {"ok": True, "path": args.get("path")}

    batch = [
        {
            "id": f"read-{index}",
            "function": {
                "name": "coding_read_file_lines",
                "arguments": json.dumps({"path": "services/gateway/app/coding_agent.py", "start_line": index + 1}),
            },
        }
        for index in range(3)
    ]
    monkeypatch.setattr(coding_agent.cw, "load_task", lambda _task_id: task)
    monkeypatch.setattr(coding_agent.cw, "mutate_task", mutate_task)
    monkeypatch.setattr(coding_agent.cw, "save_task", lambda value: value)
    monkeypatch.setattr(coding_agent.cw, "git_head", lambda _task_id: {"ok": True, "commit": "abc123"})
    monkeypatch.setattr(coding_agent.cw, "workspace_progress_fingerprint", lambda _task_id: "unchanged")
    monkeypatch.setattr(coding_agent, "_settings_for_task_owner", lambda value: {})
    monkeypatch.setattr(coding_agent, "_mission_for_task", lambda value: mission)
    monkeypatch.setattr(coding_agent, "_system_prompt", lambda *args, **kwargs: "system")
    monkeypatch.setattr(coding_agent, "_task_context", lambda value: "task")
    monkeypatch.setattr(coding_agent, "_backend_supports_tool_calling", lambda backend: True)
    monkeypatch.setattr(coding_agent, "_max_completion_tokens_for_route", lambda *args: 64)
    monkeypatch.setattr(coding_agent, "_call_backend_chat_with_retry", backend_call)
    monkeypatch.setattr(coding_agent, "_extract_assistant_message", lambda response: coding_agent.ChatMessage(role="assistant", content=None, tool_calls=batch))
    monkeypatch.setattr(coding_agent, "_extract_assistant_thinking", lambda response: "")
    monkeypatch.setattr(coding_agent, "_extract_tool_calls", lambda response: batch)
    monkeypatch.setattr(coding_agent, "_run_tool", run_tool)
    monkeypatch.setattr(coding_agent, "_checkpoint_enabled", lambda: False)
    monkeypatch.setattr(coding_agent, "_semantic_reroute_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        coding_agent,
        "decide_route",
        lambda **kwargs: SimpleNamespace(backend="test_backend", model="test_model", reason="test"),
    )

    await coding_agent._run_agent(
        "code_test",
        run_id="run",
        git_token_value=None,
        model="coder",
        auto_commit=False,
        commit_message=None,
        max_cycles=20,
        max_runtime_sec=600,
        context_reset_cycles=0,
    )

    rejected = [item for item in task["agent_events"] if item.get("type") == "forced_action_tool_rejected"]
    assert len(rejected) >= 2
    assert executed_reads < len(advertised_tool_sets) * len(batch)
    assert "coding_read_file_lines" in advertised_tool_sets[0]
    assert "coding_read_file_lines" not in advertised_tool_sets[-1]
    assert task["agent_status"] == "paused"
    assert task["agent_stop_reason_code"] == "forced_action_noncompliance"
    assert task["agent_forced_action"]["status"] == "active"


def test_noop_write_fingerprint_is_not_progress''').lstrip(),
    "replace real-loop test",
)

new_tests = dedent(r'''
from __future__ import annotations

from app import coding_agent
from app import coding_forced_action as forced
from app import coding_stagnation_resilience as resilience
from app import coding_workspace as workspace


def _task() -> dict:
    return {
        "agent_run_id": "run-2",
        "agent_cycle": 6,
        "agent_progress_state": {
            "stagnant_cycles": 6,
            "observation": {
                "workspace_fingerprint": "same",
                "validation_revision": 0,
                "diff_review_revision": 0,
                "finish_state": "running",
            },
        },
    }


def test_extracts_latest_concrete_commitment_from_model_notes():
    events = [
        {"type": "assistant", "content": "Let me inspect the helper again."},
        {"type": "assistant", "content": "I have enough evidence. I'll add the missing stale-category regression test now."},
        {"type": "assistant", "content": "Let me read the file one more time."},
    ]
    assert resilience.extract_concrete_commitment(events) == "Add the missing stale-category regression test now."


def test_forced_action_persists_across_unchanged_resume_and_expires_on_progress():
    task = _task()
    key = resilience.durable_state_key(task)
    first = forced.activate(task, state_key=key, run_id="run-2", cycle=6, stage="interrupt", required_action="Add the regression test.")
    task["agent_forced_action"] = first
    task["agent_run_id"] = "run-3"
    resumed = forced.activate(task, state_key=key, run_id="run-3", cycle=1, stage="continuation", required_action="Add the regression test.")
    task["agent_forced_action"] = resumed

    assert forced.active_state(task)["resume_count"] == 1
    task["agent_progress_state"]["observation"]["workspace_fingerprint"] = "edited"
    assert forced.active_state(task) == {}
    assert forced.retire_if_state_changed(task, state_key=resilience.durable_state_key(task)) is True
    assert task["agent_forced_action"]["status"] == "superseded"


def test_forced_action_allows_only_edits_validation_diff_or_finish():
    task = _task()
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
    )

    allowed, _ = forced.evaluate_tool_call(task, name="coding_read_file_lines", args={"path": "x.py"}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_run_command", args={"argv": ["git", "log", "-1"]}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is False
    allowed, _ = forced.evaluate_tool_call(task, name="coding_run_command", args={"argv": ["python", "-m", "pytest", "-q"]}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True
    allowed, _ = forced.evaluate_tool_call(task, name="coding_replace_text", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True
    allowed, _ = forced.evaluate_tool_call(task, name="coding_git_diff", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True
    allowed, _ = forced.evaluate_tool_call(task, name="coding_finish", args={}, is_validation_command=coding_agent._is_validation_command)
    assert allowed is True


def test_review_mission_defaults_do_not_require_changes_or_commit():
    mission = workspace.normalize_coding_mission(
        {
            "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",
            "repo_url": "https://github.com/example/repo.git",
            "base_branch": "main",
            "branch_name": "review",
        }
    )
    assert mission["completion_policy"]["require_file_changes"] is False
    assert mission["completion_policy"]["require_commit_on_success"] is False


def test_fix_mission_still_requires_changes_and_commit():
    mission = workspace.normalize_coding_mission(
        {
            "prompt": "Fix the stale qualification bug and add a regression test.",
            "repo_url": "https://github.com/example/repo.git",
            "base_branch": "main",
            "branch_name": "fix",
        }
    )
    assert mission["completion_policy"]["require_file_changes"] is True
    assert mission["completion_policy"]["require_commit_on_success"] is True
''').lstrip()
(ROOT / "services/gateway/tests/test_coding_forced_action.py").write_text(new_tests, encoding="utf-8")

# Add no-change finalization regression to the existing mission suite.
mission_test = ROOT / "services/gateway/tests/test_coding_mission_finalization.py"
replace_once(
    mission_test,
    "def test_coding_finalization_push_on_success(monkeypatch):\n",
    dedent('''
def test_review_finalization_succeeds_without_new_changes_or_commit(monkeypatch):
    stored = _finalizer_mocks(monkeypatch, changed=False)
    stored["prompt"] = "Review this workspace for concrete findings and missing tests."
    stored["mission"] = {
        "completion_policy": {
            "require_file_changes": False,
            "require_commit_on_success": False,
            "require_validation_after_edit": True,
            "require_diff_review_after_edit": True,
        }
    }
    result = ca.finalize_successful_run("task-1", finish_summary="No actionable defect found.", run_id="run-1")
    assert result["ok"] is True
    assert result["finalization_status"] == "completed"


def test_coding_finalization_push_on_success(monkeypatch):
''').lstrip(),
    "add no-change review finalization test",
)

architecture = dedent('''
# Coding forced-action enforcement

## Failure addressed

The stagnation controller could classify repeated inspection and issue assist, interrupt, and recovery guidance, but those interventions were advisory. A model could continue invoking read and search tools, and an unchanged resume could receive another run-level no-progress window. Review missions also inherited implementation-oriented change and commit requirements.

## Enforcement model

A controller-owned `nexus_coding_forced_action.v1` record is activated at interrupt, recovery, or no-progress continuation. It is keyed to the same durable state fingerprint used by stagnation recovery. While active, native tool definitions and text-tool guidance expose only focused edits, recognized validation commands, `coding_git_diff`, and `coding_finish`. The execution layer independently rejects any other requested tool.

Two rejected calls cause either a semantic backend reroute with the same restrictions or an immediate `forced_action_noncompliance` pause. Increasing cycle budgets cannot bypass this boundary. The record remains active across unchanged resumes and becomes inactive automatically when the durable state key changes.

## Required action

The controller extracts the latest explicit model commitment framed as an edit, validation, or finish action. It replaces generic required-action text only when the existing directive is generic. Assistant commitments remain provenance-marked; they are used as execution directives, not trusted findings.

## Review missions

Goals that are clearly review/audit-only default to no required file change and no required new commit. If a review produces edits, the ordinary validation, diff-review, and commit gates still apply. Fix-oriented goals retain the existing mandatory-delta behavior.

## Compatibility

Existing task fields remain optional. Stale forced-action records are ignored when their durable state key no longer matches. New no-progress continuations do not receive a recovery-counter reset; legacy metadata remains readable but is not revived into fresh continuation credit.
''').lstrip()
(ROOT / "docs/coding-forced-action-enforcement.md").write_text(architecture, encoding="utf-8")

print("forced-action enforcement patch applied")
