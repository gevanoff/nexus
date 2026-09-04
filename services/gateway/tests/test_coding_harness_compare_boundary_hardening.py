from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_compare_boundary_hardening", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fixture(path: Path, *, expected: dict) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "boundary-hardening",
                "description": "boundary hardening regression fixture",
                "repository": {"files": {"app.py": "VALUE = 1\n"}},
                "mission": "Inspect the repository and satisfy the objective.",
                "expected": expected,
                "limits": {"wall_time_sec": 30, "max_agent_steps": 8},
                "tags": ["boundary-hardening"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_run_process_bounds_output_while_draining_without_communicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = io.StringIO("x" * 10_000)
            self.stderr = io.StringIO("y" * 10_000)
            self.returncode = 0
            self.pid = 424242

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def communicate(self, *args, **kwargs):
            raise AssertionError("run_process must not accumulate output with communicate()")

    monkeypatch.setattr(harness.subprocess, "Popen", FakeProcess)

    result = harness.run_process(
        ["fake"],
        cwd=tmp_path,
        output_limit_chars=128,
        isolate_process_group=False,
    )

    assert result["ok"] is True
    assert result["output_truncated"] is True
    assert len(result["stdout"]) == 128
    assert len(result["stderr"]) == 128


def test_workspace_snapshot_preserves_non_ascii_filename_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    name = "café.txt"
    (workspace / name).write_text("bonjour\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert name in snapshot["files_changed"]
    assert (artifacts / "final-files" / name).read_text(encoding="utf-8") == "bonjour\n"
    assert not any("caf\\" in item.get("path", "") for item in snapshot["evidence_omissions"])


def test_workspace_snapshot_preserves_non_utf8_filename_bytes(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("non-UTF-8 filename regression requires POSIX surrogateescape")
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    raw_name = b"non-utf8-\xff.txt"
    name = os.fsdecode(raw_name)
    (workspace / name).write_text("retained evidence\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)
    harness.write_json(tmp_path / "snapshot.json", snapshot)

    assert name in snapshot["files_changed"]
    assert "\ufffd" not in "".join(snapshot["files_changed"])
    retained = artifacts / "final-files" / name
    assert os.fsencode(retained.name) == raw_name
    assert retained.read_text(encoding="utf-8") == "retained evidence\n"


def test_scrub_uses_collision_proof_temporary_files(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    token = "nexus-scrub-collision-secret"
    (artifacts / "x").write_text(token + "\n", encoding="utf-8")
    collision = artifacts / "x.scrub-tmp"
    collision.write_text("KEEP-ME\n", encoding="utf-8")

    omitted = harness.scrub_retained_artifacts(artifacts, [token])

    assert omitted == []
    assert token not in (artifacts / "x").read_text(encoding="utf-8")
    assert collision.read_text(encoding="utf-8") == "KEEP-ME\n"


def test_validation_command_timeout_is_typed_before_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter([10.0, 11.0])
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(times))

    def fake_run_process(*args, **kwargs):
        return {
            "ok": False,
            "returncode": None,
            "timed_out": True,
            "stdout": "",
            "stderr": "timeout after 300s",
            "duration_ms": 300000.0,
            "launch_error": None,
            "output_truncated": False,
        }

    monkeypatch.setattr(harness, "run_process", fake_run_process)
    result = harness.run_validation(
        {"expected": {"validation": [["validator"]]}},
        tmp_path,
        tmp_path / "home",
        tmp_path / "tmp",
        deadline=1000.0,
    )

    assert result["timed_out"] is True
    assert result["budget_exhausted"] is False
    assert result["passed"] is False


def test_final_outcome_preserves_validation_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", expected={"validation": [["validator"]]})

    monkeypatch.setattr(
        harness,
        "run_validation",
        lambda *args, **kwargs: {
            "commands": [{"argv": ["validator"], "ok": False, "timed_out": True}],
            "passed": False,
            "budget_exhausted": False,
            "timed_out": True,
        },
    )

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="timeout-token",
        model="coder",
        allow_mutations=False,
    )

    assert result["outcome"]["status"] == "timed_out"
    assert result["outcome"]["interrupted"] is True
    assert result["outcome"]["error"] == "validation command timed out"


def test_untrusted_artifacts_symlink_is_replaced_before_retention(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlink boundary regression requires POSIX")
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("UNCHANGED\n", encoding="utf-8")
    fake = _write_executable(
        tmp_path / "albatross",
        f"""#!/usr/bin/env python3
import os
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
workspace = pathlib.Path(os.environ['WORKSPACE_ROOT'])
link = workspace.parent / 'artifacts'
link.symlink_to(pathlib.Path({str(outside)!r}), target_is_directory=True)
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", expected={"files_changed": []})

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="artifact-root-token",
        model="coder",
        allow_mutations=False,
    )

    artifacts = Path(result["artifacts"]["run_root"]) / "artifacts"
    assert artifacts.is_dir()
    assert not artifacts.is_symlink()
    assert (artifacts / "stdout.txt").is_file()
    assert not (outside / "stdout.txt").exists()
    assert sentinel.read_text(encoding="utf-8") == "UNCHANGED\n"


def test_run_execution_directories_are_private_0700(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("mode isolation regression requires POSIX")
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
workspace = pathlib.Path(os.environ['WORKSPACE_ROOT'])
root = workspace.parent
home = pathlib.Path(os.environ['HOME'])
tmp = pathlib.Path(os.environ['TMPDIR'])
def mode(path):
    return oct(stat.S_IMODE(path.stat().st_mode))
values = {
    'run_root': mode(root),
    'workspace': mode(workspace),
    'home': mode(home),
    'tmp': mode(tmp),
    'fixture_dir': mode(root.parent),
    'run_dir': mode(root.parent.parent),
}
(workspace / 'modes.json').write_text(json.dumps(values, sort_keys=True), encoding='utf-8')
print('done')
""",
    )
    fixture = _fixture(
        tmp_path / "fixture.json",
        expected={"files_changed": ["modes.json"]},
    )

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="private-mode-token",
        model="coder",
        allow_mutations=True,
    )

    modes_path = Path(result["artifacts"]["run_root"]) / "artifacts" / "final-files" / "modes.json"
    modes = json.loads(modes_path.read_text(encoding="utf-8"))
    assert set(modes.values()) == {"0o700"}


def test_live_probe_uses_unique_ephemeral_fixture_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(
        harness,
        "albatross_version",
        lambda executable: {
            "installed": True,
            "executable": "/fake/albatross",
            "version": "2.4.0",
            "raw": "albatross 2.4.0",
        },
    )
    monkeypatch.setattr(
        harness,
        "run_process",
        lambda *args, **kwargs: {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "stdout": "albatross --print --allow-tools --eval --json",
            "stderr": "",
            "duration_ms": 0.0,
            "launch_error": None,
            "output_truncated": False,
        },
    )

    def fake_run(fixture_path: Path, **kwargs):
        assert fixture_path.exists()
        observed.append(fixture_path)
        return (
            {
                "outcome": {"exit_code": 0},
                "trajectory": {"tool_call_names": ["file_read"]},
                "objective": {"passed": True},
                "artifacts": {"trace_files": ["trace.events.jsonl"]},
            },
            tmp_path / f"result-{len(observed)}.json",
        )

    monkeypatch.setattr(harness, "run_albatross_fixture", fake_run)

    first = harness.probe(
        "albatross",
        live=True,
        out_root=tmp_path / "runs",
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="probe-token",
        model="coder",
    )
    second = harness.probe(
        "albatross",
        live=True,
        out_root=tmp_path / "runs",
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="probe-token",
        model="coder",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(observed) == 2
    assert observed[0] != observed[1]
    assert all(path.name.startswith("read-only-probe-") for path in observed)
    assert all(not path.exists() for path in observed)
