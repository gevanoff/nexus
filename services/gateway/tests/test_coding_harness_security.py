from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_security", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def test_albatross_agent_surface_omits_unsandboxed_execution(tmp_path: Path) -> None:
    env = harness.build_albatross_env(
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="test-token",
        model="coder",
        workspace=tmp_path / "work",
        home=tmp_path / "home",
        temp_dir=tmp_path / "tmp",
        max_steps=8,
    )
    tools = set(env["AGENT_TOOLS"].split(","))
    assert "shell" not in tools
    assert "run_tests" not in tools
    assert "file_edit" in tools


def test_workspace_snapshot_preserves_untracked_file_evidence(tmp_path: Path) -> None:
    fixture = {
        "repository": {"files": {"tracked.txt": "baseline\n"}}
    }
    workspace = tmp_path / "work"
    baseline = harness.initialize_workspace(workspace, fixture)
    (workspace / "created.txt").write_text("new evidence\n", encoding="utf-8")
    snapshot = harness.workspace_snapshot(workspace, baseline, tmp_path / "artifacts")
    diff = (tmp_path / "artifacts" / "final.diff").read_text(encoding="utf-8")
    assert "created.txt" in snapshot["files_changed"]
    assert "new evidence" in diff
    assert (tmp_path / "artifacts" / "final-files" / "created.txt").read_text() == "new evidence\n"
