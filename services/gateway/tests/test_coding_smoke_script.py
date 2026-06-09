from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_smoke_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "deploy" / "scripts" / "coding-agent-smoke-test.py"
    spec = importlib.util.spec_from_file_location("coding_agent_smoke_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_changed_files_from_diff_combines_workspace_and_committed_changes():
    smoke = _load_smoke_module()

    payload = {
        "changes": {"files": [{"path": "uncommitted.py"}]},
        "committed_changes": {"files": [{"path": "committed.py"}]},
    }

    assert smoke.changed_files_from_diff(payload) == {"uncommitted.py", "committed.py"}


def test_command_summary_redacts_to_tails_and_preserves_status():
    smoke = _load_smoke_module()

    payload = {
        "result": {
            "ok": False,
            "returncode": 1,
            "stdout": "a" * 3000,
            "stderr": "b" * 3000,
        }
    }

    summary = smoke.command_summary(payload)

    assert summary["ok"] is False
    assert summary["returncode"] == 1
    assert summary["stdout_tail"] == "a" * 2000
    assert summary["stderr_tail"] == "b" * 2000
