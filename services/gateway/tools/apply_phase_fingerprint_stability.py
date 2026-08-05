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
    '''    if current_phase(task) != DISCOVERY:\n        return ""\n    pending: Dict[str, Dict[str, Any]] = {}\n''',
    '''    pending: Dict[str, Dict[str, Any]] = {}\n''',
)

replace_once(
    "services/gateway/app/coding_agent.py",
    '''    if "require_file_changes" in completion:\n        return bool(completion.get("require_file_changes"))\n    return _request_expects_workspace_edits(task)\n''',
    '''    if "require_file_changes" in completion:\n        return bool(completion.get("require_file_changes"))\n    return coding_work_phases.mission_requires_file_changes(task)\n''',
)

path = ROOT / "services/gateway/tests/test_coding_work_phases.py"
text = path.read_text(encoding="utf-8")
addition = '''\n\ndef test_validation_fingerprint_survives_phase_transition():\n    task = _review_task()\n    task["agent_events"].extend(\n        [\n            {\n                "type": "tool_started",\n                "tool_call_id": "validation-stable",\n                "name": "coding_run_command",\n                "args": {"argv": ["python", "-m", "pytest", "tests/test_stable.py"]},\n            },\n            {\n                "type": "tool_finished",\n                "tool_call_id": "validation-stable",\n                "name": "coding_run_command",\n                "result": {"ok": False, "returncode": 1, "stderr": "stable failure"},\n            },\n        ]\n    )\n    discovery = phases.discovery_evidence_fingerprint(task)\n    task["agent_work_phase"] = phases.phase_state(task, phase=phases.DECISION, decision="report_only")\n\n    assert phases.discovery_evidence_fingerprint(task) == discovery\n\n\ndef test_legacy_review_audit_uses_phase_mission_fallback():\n    task = {\n        "prompt": "Review this workspace for bugs and missing tests.",\n        "agent_run_prompt": "Fix them.",\n    }\n\n    assert coding_agent._mission_requires_workspace_edits(task) is False\n'''
if "test_validation_fingerprint_survives_phase_transition" in text:
    raise RuntimeError("phase fingerprint stability tests already exist")
path.write_text(text.rstrip() + addition + "\n", encoding="utf-8")

print("Applied phase fingerprint stability fixes.")
