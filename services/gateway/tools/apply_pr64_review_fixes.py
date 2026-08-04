#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def replace_once(relative_path: str, old: str, new: str) -> None:
    path = ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{relative_path}: expected exactly one replacement target, found {count}"
        )
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(relative_path: str, marker: str, addition: str) -> None:
    path = ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    if marker in text:
        raise RuntimeError(f"{relative_path}: marker already exists: {marker}")
    suffix = "" if text.endswith("\n") else "\n"
    path.write_text(text + suffix + addition.lstrip("\n"), encoding="utf-8")


replace_once(
    "services/gateway/app/coding_workspace.py",
    r'''_DIRECT_CHANGE_RE = re.compile(
    r"\\b(fix|repair|resolve|implement|edit|modify|patch|add|remove|create|rewrite|change|update)\\b",
    re.IGNORECASE,
)''',
    r'''_DIRECT_CHANGE_RE = re.compile(
    r"\b(fix|repair|resolve|implement|edit|modify|patch|add|remove|create|rewrite|change|update)\b",
    re.IGNORECASE,
)''',
)

replace_once(
    "services/gateway/app/coding_workspace.py",
    '''    push = bool(push_on_success or draft_pr_on_success)
    return {
        "completion_policy": {
            "commit_policy": str(commit_policy or "always_on_success"),
        },''',
    '''    push = bool(push_on_success or draft_pr_on_success)
    completion_policy: Dict[str, Any] = {
        "commit_policy": str(commit_policy or "always_on_success"),
    }
    if push:
        completion_policy.update({
            "require_file_changes": True,
            "require_commit_on_success": True,
        })
    return {
        "completion_policy": completion_policy,''',
)

replace_once(
    "services/gateway/app/coding_stagnation_resilience.py",
    r'''        target = re.split(r"\\b(?:before|after|then)\\b", target, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,;-")''',
    r'''        target = re.split(r"\b(?:before|after|then)\b", target, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,;-")''',
)

replace_once(
    "services/gateway/app/coding_semantic_memory.py",
    '''        cw.mutate_task(task_id, retire_legacy_continuation)
        return False''',
    '''        updated = cw.mutate_task(task_id, retire_legacy_continuation)
        updated_lease = (
            updated.get("agent_stagnation_recovery_lease")
            if isinstance(updated.get("agent_stagnation_recovery_lease"), dict)
            else {}
        )
        if str(updated_lease.get("id") or "") == str(lease.get("id") or ""):
            task["agent_stagnation_recovery_lease"] = dict(updated_lease)
        return False''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''    completion = contract["completion_policy"]
    expects_workspace_edits = _request_expects_workspace_edits(task)
    now = time.time()''',
    '''    completion = contract["completion_policy"]
    now = time.time()''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''        change_counts = changes.get("counts") if isinstance(changes.get("counts"), dict) else {}
        has_uncommitted = int(change_counts.get("total") or 0) > 0
        base_changes = diff.get("changes") if isinstance(diff.get("changes"), dict) else {}
        base_counts = base_changes.get("counts") if isinstance(base_changes.get("counts"), dict) else {}
        actual_delta = int(base_counts.get("total") or 0) > 0 or has_uncommitted
        require_file_changes = bool(completion.get("require_file_changes", True)) and expects_workspace_edits
        require_commit_on_success = bool(completion.get("require_commit_on_success", True)) or actual_delta
        if require_file_changes and not actual_delta:
            raise RuntimeError("successful run has no meaningful delta versus the base branch")
        if actual_delta and completion.get("require_validation_after_edit", True) and not bool((snapshot.get("validation") or {}).get("validation_after_latest_edit")):
            raise RuntimeError("successful run lacks validation after the latest edit")
        if actual_delta and completion.get("require_diff_review_after_edit", True) and not bool((snapshot.get("diff_review") or {}).get("diff_reviewed_after_latest_edit")):
            raise RuntimeError("successful run lacks diff review after the latest edit")''',
    '''        change_counts = changes.get("counts") if isinstance(changes.get("counts"), dict) else {}
        has_uncommitted = int(change_counts.get("total") or 0) > 0
        latest = cw.load_task(task_id)
        current_head = str(before.get("commit") or "")
        start_head = str(latest.get("agent_start_head") or "")
        checkpoint_for_run = str(latest.get("last_checkpoint_run_id") or "") == str(run_id or "")
        committed_run_delta = bool(
            (start_head and current_head and current_head != start_head)
            or (
                checkpoint_for_run
                and current_head
                and str(latest.get("last_checkpoint_commit") or "") == current_head
            )
        )
        run_delta = has_uncommitted or committed_run_delta
        require_file_changes = bool(completion.get("require_file_changes", True))
        require_commit_on_success = bool(completion.get("require_commit_on_success", True)) or run_delta
        if require_file_changes and not run_delta:
            raise RuntimeError("successful run has no meaningful delta produced by this run")
        if run_delta and completion.get("require_validation_after_edit", True) and not bool((snapshot.get("validation") or {}).get("validation_after_latest_edit")):
            raise RuntimeError("successful run lacks validation after the latest edit")
        if run_delta and completion.get("require_diff_review_after_edit", True) and not bool((snapshot.get("diff_review") or {}).get("diff_reviewed_after_latest_edit")):
            raise RuntimeError("successful run lacks diff review after the latest edit")''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''        else:
            latest = cw.load_task(task_id)
            after = cw.git_head(task_id)
            candidate = str(after.get("commit") or latest.get("last_checkpoint_commit") or latest.get("last_commit") or "")
            start_head = str(latest.get("agent_start_head") or "")
            checkpoint_for_run = str(latest.get("last_checkpoint_run_id") or "") == str(run_id or "")
            if require_file_changes and not candidate:
                raise RuntimeError("successful run has no branch commit")
            if require_file_changes and start_head and candidate == start_head and not checkpoint_for_run:
                raise RuntimeError("successful run produced no commit after run start")
            result["final_commit"] = candidate
            result["committed_at"] = float(latest.get("last_checkpoint_at") or latest.get("updated_at") or now)''',
    '''        else:
            candidate = str(current_head or latest.get("last_checkpoint_commit") or latest.get("last_commit") or "")
            if require_file_changes and not candidate:
                raise RuntimeError("successful run has no branch commit")
            result["final_commit"] = candidate
            result["committed_at"] = float(latest.get("last_checkpoint_at") or latest.get("updated_at") or now)''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''    guidance = [
        "Use coding_tool_manifest when you need to inspect your workspace tool capabilities.",
        "Commands run inside a Linux workspace shell. Use POSIX paths, forward slashes, and Linux command/env syntax such as ls, cat, grep, python3, VAR=value cmd, and $VAR.",
        "Do not assume PowerShell, cmd.exe, drive letters, backslashes, %VAR%, or $env:VAR inside the workspace.",
        "Use coding_list_tree, coding_search_text, and coding_read_file_lines before broad reads or edits.",
        "For work spanning several milestones, use coding_update_plan and keep milestone statuses current.",
        "Prefer coding_replace_text for exact focused edits and coding_apply_patch for multi-file diffs.",
        "Use coding_fetch_url for current public documentation or issue pages.",
        "Do not invent imports, functions, methods, variables, or config keys; search and read definitions before using them.",
        "Keep imports consolidated and avoid loading the same library multiple times.",
        "If a service owns its own package root, run validation from that service directory, for example cwd=services/gateway for gateway tests that import app.",
        "After editing, run a targeted validation command such as pytest, ruff check, python -m py_compile, node --check, npm test, or git diff --check.",
        "Do not invent package.json files, lockfiles, requirements files, or placeholder tests just to make validation pass. Only add project-manifest or dependency files when the user explicitly asked for that scaffolding or the target service already uses it.",
        "Placeholder handlers or comments like 'Add logic to ...' do not count as a fix.",
        "After editing, inspect coding_git_diff before calling coding_finish.",
    ]''',
    '''    if isinstance(task, dict) and forced_action.active_state(task):
        guidance = [
            "Controller forced-action mode is active for the unchanged durable state.",
            "Inspection, orientation, plan, and arbitrary shell tools are unavailable.",
            "Make the required focused edit, or run a recognized targeted validation command.",
            "After editing, call coding_git_diff and then coding_finish.",
        ]
    else:
        guidance = [
            "Use coding_tool_manifest when you need to inspect your workspace tool capabilities.",
            "Commands run inside a Linux workspace shell. Use POSIX paths, forward slashes, and Linux command/env syntax such as ls, cat, grep, python3, VAR=value cmd, and $VAR.",
            "Do not assume PowerShell, cmd.exe, drive letters, backslashes, %VAR%, or $env:VAR inside the workspace.",
            "Use coding_list_tree, coding_search_text, and coding_read_file_lines before broad reads or edits.",
            "For work spanning several milestones, use coding_update_plan and keep milestone statuses current.",
            "Prefer coding_replace_text for exact focused edits and coding_apply_patch for multi-file diffs.",
            "Use coding_fetch_url for current public documentation or issue pages.",
            "Do not invent imports, functions, methods, variables, or config keys; search and read definitions before using them.",
            "Keep imports consolidated and avoid loading the same library multiple times.",
            "If a service owns its own package root, run validation from that service directory, for example cwd=services/gateway for gateway tests that import app.",
            "After editing, run a targeted validation command such as pytest, ruff check, python -m py_compile, node --check, npm test, or git diff --check.",
            "Do not invent package.json files, lockfiles, requirements files, or placeholder tests just to make validation pass. Only add project-manifest or dependency files when the user explicitly asked for that scaffolding or the target service already uses it.",
            "Placeholder handlers or comments like 'Add logic to ...' do not count as a fix.",
            "After editing, inspect coding_git_diff before calling coding_finish.",
        ]''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''    names = ", ".join(tools)
    return (
        "This selected backend does not receive native OpenAI tool definitions. "
        "Use text-form tool calls instead. To call a tool, respond with exactly one complete block and no prose: "
        '<tool_call>{"name":"coding_read_file_lines","arguments":{"path":"README.md","start_line":1,"line_count":80}}</tool_call>. '
        "Use JSON only inside the block. Do not wrap the block in Markdown fences. "
        "Call coding_tool_manifest with include_parameters=true if you need exact parameter schemas. "
        f"Available tool names: {names}."
    )''',
    '''    names = ", ".join(tools)
    if "coding_read_file_lines" in tools:
        example = '<tool_call>{"name":"coding_read_file_lines","arguments":{"path":"README.md","start_line":1,"line_count":80}}</tool_call>. '
    elif "coding_git_diff" in tools:
        example = '<tool_call>{"name":"coding_git_diff","arguments":{}}</tool_call>. '
    elif "coding_finish" in tools:
        example = '<tool_call>{"name":"coding_finish","arguments":{"summary":"Blocked","success":false}}</tool_call>. '
    else:
        example = ""
    manifest_hint = (
        "Call coding_tool_manifest with include_parameters=true if you need exact parameter schemas. "
        if "coding_tool_manifest" in tools
        else ""
    )
    return (
        "This selected backend does not receive native OpenAI tool definitions. "
        "Use text-form tool calls instead. To call a tool, respond with exactly one complete block and no prose: "
        f"{example}"
        "Use JSON only inside the block. Do not wrap the block in Markdown fences. "
        f"{manifest_hint}"
        f"Available tool names: {names}."
    )''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''    forced_context = _forced_action_context(task)
    if forced_context:
        edit_expectation += forced_context + " "
    if text_tool_mode:''',
    '''    forced_context = _forced_action_context(task)
    if forced_context:
        text_call_guidance = f"{_text_tool_call_guidance(task)} " if text_tool_mode else ""
        return (
            "You are Nexus Coding Agent in controller-enforced forced-action mode. "
            "Work by calling workspace tools, not by narrating. "
            f"{text_call_guidance}"
            "Do not inspect, orient, revise the project plan, or run arbitrary shell commands. "
            "Use only a focused edit, a recognized targeted validation command, coding_git_diff, or coding_finish. "
            "Do not push or open pull requests directly. Nexus performs successful finalization according to the mission contract. "
            f"{forced_context} "
            f"Allowed commands: {allowed or '(none)'}. "
            f"Workspace task id: {task.get('id')}. Base branch: {task.get('base_branch')}. Working branch: {task.get('branch_name')}.\n\n"
            + "\n\n".join(request_bits)
        )
    if text_tool_mode:''',
)

append_once(
    "services/gateway/tests/test_coding_forced_action.py",
    "test_forced_action_prompts_only_advertise_allowed_actions",
    r'''

def test_commitment_extraction_stops_before_transition_clause():
    events = [
        {
            "type": "assistant",
            "content": "I have enough evidence. I'll add the stale-category regression test before running pytest.",
        }
    ]

    assert resilience.extract_concrete_commitment(events) == "Add the stale-category regression test."


def test_mixed_review_and_fix_goal_still_requires_changes():
    mission = workspace.normalize_coding_mission(
        {
            "prompt": "Review this workspace and fix any bugs you find.",
            "repo_url": "https://github.com/example/repo.git",
            "base_branch": "main",
            "branch_name": "review-fix",
        }
    )

    assert mission["completion_policy"]["require_file_changes"] is True
    assert mission["completion_policy"]["require_commit_on_success"] is True


def test_publish_overrides_require_a_run_delta_and_commit():
    mission = workspace.coding_mission_overrides(push_on_success=True)

    assert mission["completion_policy"]["require_file_changes"] is True
    assert mission["completion_policy"]["require_commit_on_success"] is True


def test_forced_action_prompts_only_advertise_allowed_actions():
    task = _task()
    task.update(
        {
            "id": "code_forced_prompt",
            "prompt": "Add the regression test.",
            "base_branch": "main",
            "branch_name": "forced",
            "project_plan": {"items": []},
        }
    )
    key = resilience.durable_state_key(task)
    task["agent_forced_action"] = forced.activate(
        task,
        state_key=key,
        run_id="run-2",
        cycle=6,
        stage="interrupt",
        required_action="Add the regression test.",
    )

    native_prompt = coding_agent._system_prompt(task)
    text_prompt = coding_agent._system_prompt(task, text_tool_mode=True)
    manifest = coding_agent.coding_tool_manifest(task)
    text_guidance = coding_agent._text_tool_call_guidance(task)

    for rendered in (native_prompt, text_prompt, text_guidance):
        assert "coding_search_text" not in rendered
        assert "coding_read_file_lines" not in rendered
        assert "coding_update_plan" not in rendered
    assert "coding_git_diff" in text_prompt
    assert "coding_git_diff" in text_guidance
    assert all("coding_search_text" not in item for item in manifest["guidance"])
    assert set(manifest["tool_names"]) == forced.allowed_tool_names(task)
''',
)

append_once(
    "services/gateway/tests/test_coding_mission_finalization.py",
    "test_finalization_preserves_explicit_required_change_contract",
    r'''

def test_finalization_preserves_explicit_required_change_contract(monkeypatch):
    stored = _finalizer_mocks(monkeypatch, changed=False, base_delta=False)
    stored["prompt"] = "Add a regression test."
    stored["agent_start_head"] = "start"
    stored["last_checkpoint_run_id"] = "run-0"
    stored["last_checkpoint_commit"] = "start"
    stored["mission"] = {
        "completion_policy": {
            "require_file_changes": True,
            "require_commit_on_success": True,
            "require_validation_after_edit": True,
            "require_diff_review_after_edit": True,
        }
    }
    monkeypatch.setattr(cw, "git_head", lambda *_a, **_k: {"ok": True, "commit": "start"})

    result = ca.finalize_successful_run("task-1", finish_summary="Nothing changed.", run_id="run-1")

    assert result["ok"] is False
    assert "delta produced by this run" in result["finalization_error"]


def test_review_finalization_ignores_preexisting_branch_delta(monkeypatch):
    stored = _finalizer_mocks(monkeypatch, changed=False, base_delta=True)
    stored["prompt"] = "Review this workspace for concrete findings and missing tests."
    stored["agent_start_head"] = "review-head"
    stored["last_checkpoint_run_id"] = "run-0"
    stored["last_checkpoint_commit"] = "review-head"
    stored["mission"] = {
        "completion_policy": {
            "require_file_changes": False,
            "require_commit_on_success": False,
            "require_validation_after_edit": True,
            "require_diff_review_after_edit": True,
        }
    }
    monkeypatch.setattr(cw, "git_head", lambda *_a, **_k: {"ok": True, "commit": "review-head"})
    monkeypatch.setattr(
        cw,
        "coding_state_snapshot",
        lambda *_a, **_k: {
            "validation": {"validation_after_latest_edit": False},
            "diff_review": {"diff_reviewed_after_latest_edit": False},
        },
    )

    result = ca.finalize_successful_run(
        "task-1",
        finish_summary="No actionable defect found.",
        run_id="run-1",
    )

    assert result["ok"] is True
    assert result["finalization_status"] == "completed"
''',
)

append_once(
    "services/gateway/tests/test_coding_stagnation_resilience.py",
    "test_retired_continuation_lease_updates_detached_task_sample",
    r'''

def test_retired_continuation_lease_updates_detached_task_sample(monkeypatch):
    persisted = _task(stagnant_cycles=8)
    key = resilience.durable_state_key(persisted)
    persisted["agent_stagnation_recovery_lease"] = {
        "schema": "nexus_coding_recovery_lease.v1",
        "id": f"{key}:legacy-continuation",
        "state_key": key,
        "kind": "continuation",
        "run_id": "run-2",
        "granted_cycle": 1,
        "remaining_transitions": 1,
        "status": "granted",
    }
    persisted["agent_cycle"] = 2

    def detached_load(_task_id):
        import copy
        return copy.deepcopy(persisted)

    def detached_mutate(_task_id, mutator):
        import copy
        latest = copy.deepcopy(persisted)
        mutator(latest)
        persisted.clear()
        persisted.update(copy.deepcopy(latest))
        return copy.deepcopy(latest)

    monkeypatch.setattr(memory.cw, "load_task", detached_load)
    monkeypatch.setattr(memory.cw, "mutate_task", detached_mutate)
    sample = detached_load(persisted["id"])

    assert memory._consume_recovery_lease(persisted["id"], sample) is False
    assert persisted["agent_stagnation_recovery_lease"]["status"] == "superseded"
    assert sample["agent_stagnation_recovery_lease"]["status"] == "superseded"
''',
)

print("Applied PR #64 review fixes.")
