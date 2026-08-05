from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def replace_once(relative: str, old: str, new: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{relative}: expected one target, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "services/gateway/app/coding_work_phases.py",
    '''from typing import Any, Dict, Mapping, Sequence\n\n\nSCHEMA''',
    '''from typing import Any, Dict, Mapping, Sequence\n\nfrom app import coding_workspace as workspace\n\n\nSCHEMA''',
)
replace_once(
    "services/gateway/app/coding_work_phases.py",
    '''    if "require_file_changes" in completion:\n        return bool(completion.get("require_file_changes"))\n    return True\n''',
    '''    if "require_file_changes" in completion:\n        return bool(completion.get("require_file_changes"))\n    goal = str(mission.get("goal") or task.get("prompt") or task.get("agent_run_prompt") or "")\n    return workspace.goal_expects_file_changes(goal)\n''',
)
replace_once(
    "services/gateway/app/coding_work_phases.py",
    '''        if not run_id or not event_run_id or event_run_id == run_id:\n            start = index\n            break\n''',
    '''        if event_run_id == run_id:\n            start = index\n            break\n''',
)
replace_once(
    "services/gateway/app/coding_work_phases.py",
    '''    if command in {"npm", "pnpm", "yarn"}:\n        return any(marker in item for item in lowered[1:] for marker in ("test", "lint", "typecheck", "check", "build"))\n    if command == "uv":\n        meaningful = {item for item in lowered[1:] if not item.startswith("-")}\n        return bool(\n            meaningful.intersection({"pytest", "ruff", "mypy", "py_compile", "compileall", "unittest"})\n            or "--check" in lowered\n            or "--test" in lowered\n            or any(marker in item for item in meaningful for marker in ("test", "lint", "typecheck"))\n        )\n''',
    '''    if command in {"npm", "pnpm", "yarn"}:\n        arguments = [item for item in lowered[1:] if not item.startswith("-")]\n        if not arguments:\n            return False\n        validation_scripts = {"test", "lint", "typecheck", "check", "build"}\n        if arguments[0] in validation_scripts:\n            return True\n        return (\n            arguments[0] in {"run", "run-script"}\n            and len(arguments) > 1\n            and (arguments[1] in validation_scripts or arguments[1].split(":", 1)[0] in validation_scripts)\n        )\n    if command == "uv":\n        arguments = [item for item in parts[1:] if not item.startswith("-")]\n        if arguments and arguments[0].lower() == "run":\n            arguments = arguments[1:]\n        if not arguments:\n            return False\n        nested = Path(arguments[0]).name.lower()\n        if nested in {"pytest", "ruff", "mypy"}:\n            return True\n        if nested in {"python", "python3"}:\n            return _python_validation_command(arguments[1:])\n        return nested == "git" and [item.lower() for item in arguments[1:]] in (["diff", "--check"], ["diff", "--cached", "--check"])\n''',
)

replace_once(
    "services/gateway/app/coding_stagnation_resilience.py",
    '''    review_only = not mission_requires_file_changes(task)\n''',
    '''    review_only = not work_phases.mission_requires_file_changes(task)\n''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''    if _request_expects_workspace_edits(task):\n        edit_expectation = (\n''',
    '''    if _mission_requires_workspace_edits(task):\n        edit_expectation = (\n''',
)

path = ROOT / "services/gateway/tests/test_coding_work_phases.py"
text = path.read_text(encoding="utf-8")
addition = '''\n\ndef test_legacy_review_task_derives_report_only_policy():\n    task = {\n        "prompt": "Review this workspace for bugs, behavioral regressions, risky assumptions, and missing tests.",\n        "agent_run_id": "run-legacy",\n        "agent_events": [{"type": "started", "run_id": "run-legacy"}],\n    }\n\n    assert phases.mission_requires_file_changes(task) is False\n    decision = phases.advance_phase(task, stage="interrupt")\n    assert decision["phase"] == phases.DECISION\n    assert decision["decision"] == "report_only"\n\n\ndef test_package_manager_installs_are_not_validation_evidence():\n    assert phases._is_validation_command(["npm", "install", "lint-staged"]) is False\n    assert phases._is_validation_command(["yarn", "add", "check-deps"]) is False\n    assert phases._is_validation_command(["npm", "run", "test:unit"]) is True\n    assert phases._is_validation_command(["pnpm", "lint"]) is True\n\n\ndef test_empty_started_event_does_not_hide_current_run_validation():\n    task = _review_task()\n    task["agent_events"].extend(\n        [\n            {\n                "type": "tool_started",\n                "tool_call_id": "validation-current",\n                "name": "coding_run_command",\n                "args": {"argv": ["python", "-m", "pytest", "tests/test_current.py"]},\n            },\n            {\n                "type": "tool_finished",\n                "tool_call_id": "validation-current",\n                "name": "coding_run_command",\n                "result": {"ok": False, "returncode": 1, "stderr": "current failure"},\n            },\n            {"type": "started", "run_id": ""},\n        ]\n    )\n\n    assert phases.discovery_evidence_fingerprint(task)\n\n\ndef test_report_only_mission_prompt_does_not_advertise_fix_expectation():\n    task = _review_task()\n    task.update(\n        {\n            "id": "code_phase_prompt",\n            "base_branch": "main",\n            "branch_name": "review",\n            "agent_run_prompt": "Fix them.",\n            "project_plan": {"items": []},\n        }\n    )\n\n    rendered = coding_agent._system_prompt(task)\n\n    assert "This request is fix-oriented" not in rendered\n'''
if "test_legacy_review_task_derives_report_only_policy" in text:
    raise RuntimeError("phase compatibility tests already exist")
path.write_text(text.rstrip() + addition + "\n", encoding="utf-8")

print("Applied phase review compatibility fixes.")
