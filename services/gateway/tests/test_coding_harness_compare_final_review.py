from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_compare_final_review", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fixture(path: Path, *, files: dict[str, str], expected: dict, mission: str = "Complete the task.") -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "final-review",
                "description": "final review regression fixture",
                "repository": {"files": files},
                "mission": mission,
                "expected": expected,
                "limits": {"wall_time_sec": 30, "max_agent_steps": 8},
                "tags": ["final-review"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _workspace_fixture(files: dict[str, str]) -> dict:
    return {"repository": {"files": files}}


def test_albatross_discovery_does_not_inherit_ambient_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import os
import sys
for key in ('AWS_SECRET_ACCESS_KEY', 'GH_TOKEN', 'SSH_AUTH_SOCK'):
    if os.environ.get(key):
        print('ambient credential leaked: ' + key)
        raise SystemExit(9)
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools --eval --json')
    raise SystemExit(0)
raise SystemExit(0)
""",
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-aws-secret")
    monkeypatch.setenv("GH_TOKEN", "ambient-github-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ambient-agent.sock")

    version = harness.albatross_version(str(fake))
    report = harness.probe(str(fake), live=False)

    assert version["installed"] is True
    assert version["version"] == "2.4.0"
    assert report["ok"] is True
    assert report["capabilities"]["one_shot"] is True
    assert report["capabilities"]["allow_tools"] is True


def test_workspace_snapshot_fails_closed_when_git_metadata_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    harness.discard_path_verified(workspace / ".git")

    with pytest.raises(RuntimeError, match="snapshot Git metadata"):
        harness.workspace_snapshot(workspace, baseline, artifacts)

    assert not artifacts.exists()


def test_workspace_snapshot_fails_closed_when_git_evidence_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    (workspace / "app.py").write_text("VALUE = '" + ("x" * 4096) + "'\n", encoding="utf-8")
    monkeypatch.setattr(harness, "MAX_GIT_EVIDENCE_CHARS", 128)

    with pytest.raises(RuntimeError, match="evidence limit"):
        harness.workspace_snapshot(workspace, baseline, artifacts)


def test_symlinked_workspace_file_is_never_read_or_retained(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlink security regression requires POSIX")
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("HOST_SECRET_SHOULD_NOT_ESCAPE\n", encoding="utf-8")
    (workspace / "leak.txt").symlink_to(outside)

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)
    objective = harness.objective_checks(
        {"expected": {"file_contains": [{"path": "leak.txt", "needle": "HOST_SECRET_SHOULD_NOT_ESCAPE"}]}},
        workspace,
        snapshot["files_changed"],
        {"passed": None},
    )

    assert "leak.txt" in snapshot["files_changed"]
    assert not (artifacts / "final-files" / "leak.txt").exists()
    assert "HOST_SECRET_SHOULD_NOT_ESCAPE" not in (artifacts / "final.diff").read_text(encoding="utf-8")
    assert any(item["path"] == "leak.txt" and "symlink" in item["reason"] for item in snapshot["evidence_omissions"])
    assert objective["passed"] is False
    assert "symlink" in objective["checks"][0]["error"]


def test_trace_parser_does_not_follow_symlinked_trace_files(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlink security regression requires POSIX")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    outside = tmp_path / "outside.events.jsonl"
    outside.write_text(json.dumps({"turn": 1, "kind": "toolCall", "name": "file_read"}) + "\n", encoding="utf-8")
    (sessions / "leak.events.jsonl").symlink_to(outside)

    result = harness.parse_trace(sessions, artifact_dir=tmp_path / "artifacts")

    assert result["tool_calls"] == []
    assert result["trace_files"] == []


def test_binary_git_evidence_never_retains_reconstructable_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    token = "nexus-binary-diff-secret"
    (workspace / "leak.bin").write_bytes(b"\x00\xffprefix" + token.encode("utf-8") + b"suffix\x00\xfe")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts, secrets=[token])
    omitted = harness.scrub_retained_artifacts(artifacts, [token])
    harness.refresh_diff_metadata(snapshot, artifacts / "final.diff")
    diff = (artifacts / "final.diff").read_text(encoding="utf-8")

    assert "GIT binary patch" not in diff
    assert token not in diff
    assert "leak.bin" in snapshot["files_changed"]
    assert "final-files/leak.bin" in omitted
    assert not (artifacts / "final-files" / "leak.bin").exists()
    for path in artifacts.rglob("*"):
        if path.is_file():
            assert token.encode("utf-8") not in path.read_bytes()


def test_validation_cannot_supply_measured_workspace_mutation(tmp_path: Path) -> None:
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)
print('author complete')
""",
    )
    validation_code = "from pathlib import Path; Path('app.py').write_text('AFTER_VALIDATION\\n', encoding='utf-8')"
    fixture = _fixture(
        tmp_path / "fixture.json",
        files={"app.py": "BEFORE\n"},
        expected={
            "files_changed": ["app.py"],
            "file_contains": [{"path": "app.py", "needle": "AFTER_VALIDATION"}],
            "validation": [["python3", "-c", validation_code]],
        },
    )

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="validation-order-token",
        model="coder",
        allow_mutations=True,
    )

    assert result["outcome"]["completed"] is False
    assert result["validation"]["passed"] is False
    assert result["workspace"]["files_changed"] == []
    diff = Path(result["artifacts"]["diff"]).read_text(encoding="utf-8")
    assert diff == ""
    final_file = Path(result["artifacts"]["run_root"]) / "artifacts" / "final-files" / "app.py"
    assert not final_file.exists()


def test_repository_diff_external_and_fsmonitor_config_cannot_execute(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("external helper regression requires POSIX")
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    marker = tmp_path / "external-helper-ran"
    helper = _write_executable(
        tmp_path / "helper.py",
        f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
    )
    assert harness.git(["config", "diff.external", str(helper)], cwd=workspace)["ok"] is True
    assert harness.git(["config", "core.fsmonitor", str(helper)], cwd=workspace)["ok"] is True
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert snapshot["files_changed"] == ["app.py"]
    assert not marker.exists()
    assert "+VALUE = 2" in (artifacts / "final.diff").read_text(encoding="utf-8")


def test_workspace_snapshot_preserves_trailing_whitespace_in_git_diff(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    (workspace / "app.py").write_text("VALUE = 2  \n   \n", encoding="utf-8")
    expected = harness._required_git(
        ["diff", "--no-ext-diff", "--no-textconv", baseline], cwd=workspace
    )["stdout"]

    harness.workspace_snapshot(workspace, baseline, artifacts)

    retained = (artifacts / "final.diff").read_text(encoding="utf-8")
    assert retained == expected
    assert "+VALUE = 2  \n+   \n" in retained


def test_secret_bearing_changed_filename_is_not_used_as_retained_artifact_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(workspace, _workspace_fixture({"app.py": "VALUE = 1\n"}))
    token = "nexus-secret-filename-token"
    (workspace / f"evidence-{token}.txt").write_text("safe content\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts, secrets=[token])
    harness.scrub_retained_artifacts(artifacts, [token])

    assert snapshot["files_changed"] == ["(redacted)"]
    assert any("(redacted)" in item["path"] for item in snapshot["evidence_omissions"])
    for path in artifacts.rglob("*"):
        assert token not in str(path)
    assert token not in (artifacts / "final.diff").read_text(encoding="utf-8")
