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


def test_workspace_snapshot_safely_recodes_non_utf8_diff_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    raw_content = b"invalid text byte: \xff\n"
    (workspace / "invalid.txt").write_bytes(raw_content)

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert "invalid.txt" in snapshot["files_changed"]
    assert r"\xff" in (artifacts / "final.diff").read_text(encoding="utf-8")
    assert (artifacts / "final-files" / "invalid.txt").read_bytes() == raw_content


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


def test_scrub_discards_encoded_secret_artifacts(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    token = "nexus-encoded-retention-secret"
    encoded = artifacts / "encoded.txt"
    encoded.write_bytes(token.encode("utf-16-le"))

    omitted = harness.scrub_retained_artifacts(artifacts, [token])

    assert omitted == ["encoded.txt"]
    assert not encoded.exists()


def test_scrub_discards_mixed_case_hexadecimal_secrets(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    token = "nexus-mixed-case-hex-secret"
    raw_hex = token.encode("utf-8").hex()
    mixed_case_hex = "".join(
        value.upper() if index % 2 else value
        for index, value in enumerate(raw_hex)
    )
    encoded = artifacts / "encoded.txt"
    encoded.write_text(mixed_case_hex + "\n", encoding="ascii")

    omitted = harness.scrub_retained_artifacts(artifacts, [token])

    assert omitted == ["encoded.txt"]
    assert not encoded.exists()


def test_scrub_and_result_redaction_detect_line_wrapped_base64_secrets(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    token = "nexus-line-wrapped-base64-secret-" * 3
    wrapped = harness.base64.encodebytes(token.encode("utf-8"))
    encoded = artifacts / "encoded.txt"
    encoded.write_bytes(b"before\n" + wrapped + b"after\n")

    omitted = harness.scrub_retained_artifacts(artifacts, [token])

    assert omitted == ["encoded.txt"]
    assert not encoded.exists()
    assert harness.redact_text(wrapped.decode("ascii"), [token]) == "(redacted)"


def test_encoded_process_output_is_redacted_from_the_complete_result(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import base64
import os
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)
encoded = base64.b64encode(os.environ['OPENAI_API_KEY'].encode('utf-8')).decode('ascii')
print('failure: ' + encoded, file=sys.stderr)
raise SystemExit(2)
""",
    )
    token = "nexus-encoded-result-secret"
    encoded = harness.base64.b64encode(token.encode("utf-8")).decode("ascii")
    fixture = _fixture(tmp_path / "fixture.json", expected={"files_changed": []})

    result, result_path = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
        allow_mutations=False,
    )

    assert result["outcome"]["completed"] is False
    assert result["outcome"]["error"] == "(redacted)"
    assert encoded not in json.dumps(result)
    assert encoded not in result_path.read_text(encoding="utf-8")


def test_encoded_secret_paths_are_never_retained_or_reported(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import base64
import os
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)
workspace = pathlib.Path(os.environ['WORKSPACE_ROOT'])
raw = os.environ['OPENAI_API_KEY'].encode('utf-8')
(workspace / raw.hex()).write_text('hex path\\n', encoding='utf-8')
(workspace / base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')).write_text(
    'base64 path\\n', encoding='utf-8'
)
print('done')
""",
    )
    token = "nexus-encoded-path-secret"
    encoded_names = {
        token.encode("utf-8").hex(),
        harness.base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("="),
    }
    fixture = _fixture(tmp_path / "fixture.json", expected={"files_changed": []})

    result, result_path = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
        allow_mutations=True,
    )

    assert result["outcome"]["completed"] is False
    assert result["workspace"]["files_changed"] == ["(redacted)"]
    assert any(
        item["path"] == "(redacted)" and "encoding" in item["reason"]
        for item in result["workspace"]["evidence_omissions"]
    )
    retained = result_path.parent
    for path in retained.rglob("*"):
        assert not any(encoded in str(path.relative_to(retained)) for encoded in encoded_names)
        if path.is_file():
            content = path.read_bytes()
            assert not any(encoded.encode("ascii") in content for encoded in encoded_names)


def test_workspace_snapshot_pins_git_work_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    alternate = tmp_path / "alternate"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    alternate.mkdir()
    (alternate / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    configured = harness.run_process(
        ["git", "config", "core.worktree", str(alternate)],
        cwd=workspace,
    )
    assert configured["ok"] is True
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert snapshot["files_changed"] == ["app.py"]
    assert "+VALUE = 2" in (artifacts / "final.diff").read_text(encoding="utf-8")


def test_workspace_snapshot_overrides_core_file_mode(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("file-mode regression requires POSIX mode bits")
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"script.sh": "#!/bin/sh\nexit 0\n"}}},
    )
    configured = harness.run_process(
        ["git", "config", "core.fileMode", "false"],
        cwd=workspace,
    )
    assert configured["ok"] is True
    script = workspace / "script.sh"
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert snapshot["files_changed"] == ["script.sh"]
    assert "new mode 100755" in (artifacts / "final.diff").read_text(encoding="utf-8")


def test_workspace_snapshot_never_executes_agent_git_filters(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    outside = tmp_path / "operator-secret.txt"
    outside.write_text("UNKNOWN_OPERATOR_SECRET\n", encoding="utf-8")
    marker = tmp_path / "filter-ran.txt"
    filter_script = _write_executable(
        tmp_path / "leak-filter",
        f"#!/bin/sh\ncat {str(outside)!r}\ntouch {str(marker)!r}\n",
    )
    configured = harness.run_process(
        ["git", "config", "filter.leak.clean", str(filter_script)],
        cwd=workspace,
    )
    assert configured["ok"] is True
    (workspace / ".git" / "info" / "attributes").write_text(
        "app.py filter=leak\n", encoding="utf-8"
    )
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)
    diff = (artifacts / "final.diff").read_text(encoding="utf-8")

    assert snapshot["files_changed"] == ["app.py"]
    assert "+VALUE = 2" in diff
    assert "UNKNOWN_OPERATOR_SECRET" not in diff
    assert not marker.exists()


def test_validation_scratch_mount_follows_recursive_read_only_remount(
    tmp_path: Path,
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("bubblewrap sandbox regression requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("bubblewrap sandbox regression requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()

    argv, _ = harness._validation_sandbox_argv(
        ["python3", "-c", "pass"], workspace, home, temp_dir
    )

    remount_index = argv.index("--remount-ro")
    scratch_index = next(
        index
        for index in range(len(argv) - 2)
        if argv[index:index + 3] == ["--bind", str(temp_dir.resolve()), "/tmp"]
    )
    workspace_index = next(
        index
        for index in range(len(argv) - 2)
        if argv[index:index + 3]
        == ["--ro-bind", str(workspace.resolve()), str(workspace.resolve())]
    )
    assert remount_index < scratch_index < workspace_index


def test_validation_runs_in_filesystem_and_network_sandbox(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("bubblewrap sandbox regression requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("bubblewrap sandbox regression requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    outside = tmp_path / "operator-secret.txt"
    outside.write_text("must-not-be-readable\n", encoding="utf-8")
    (workspace / "fixture_module.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    host_net_namespace = os.readlink("/proc/self/ns/net")
    script = (
        "import fixture_module,os,pathlib; "
        f"outside=pathlib.Path({str(outside)!r}); "
        "tmp=pathlib.Path(os.environ['TMPDIR'])/'validation.tmp'; "
        "home=pathlib.Path(os.environ['HOME'])/'validation.home'; "
        "tmp.write_text('temporary', encoding='utf-8'); "
        "home.write_text('home', encoding='utf-8'); "
        "print('file-blocked' if not outside.exists() else outside.read_text()); "
        f"print('network-isolated' if os.readlink('/proc/self/ns/net') != {host_net_namespace!r} "
        "else 'network-shared'); print(fixture_module.VALUE); "
        "print(tmp.read_text(encoding='utf-8'), home.read_text(encoding='utf-8'))"
    )

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", script]]}},
        workspace,
        home,
        temp_dir,
        deadline=harness.time.monotonic() + 10.0,
    )

    assert result["passed"] is True, result["commands"][0]["stderr"]
    assert result["commands"][0]["stdout"].splitlines() == [
        "file-blocked", "network-isolated", "loaded", "temporary home",
    ]
    assert not list(workspace.rglob("*.pyc"))
    assert not list(workspace.rglob("__pycache__"))


def test_validation_fails_closed_without_bubblewrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    monkeypatch.setattr(harness.shutil, "which", lambda *args, **kwargs: None)

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", "print('unsafe')"]]}},
        workspace,
        home,
        temp_dir,
        deadline=harness.time.monotonic() + 10.0,
    )

    assert result["passed"] is False
    assert result["commands"][0]["launch_error"] == "validation_sandbox_unavailable"
    assert "requires bubblewrap" in result["commands"][0]["stderr"]


def test_validation_command_timeout_is_typed_before_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
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
    monkeypatch.setattr(
        harness,
        "_validation_sandbox_argv",
        lambda argv, workspace, home, temp_dir: (argv, {}),
    )
    result = harness.run_validation(
        {"expected": {"validation": [["validator"]]}},
        workspace,
        home,
        temp_dir,
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
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
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


def test_agent_timeout_gets_a_separate_bounded_trace_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path / "fixture.json", expected={"files_changed": []})
    original_run_process = harness.run_process
    original_monotonic = harness.time.monotonic
    clock_offset = [0.0]

    monkeypatch.setattr(
        harness,
        "albatross_version",
        lambda executable: {
            "installed": True,
            "executable": "fake-albatross",
            "version": "2.4.0",
        },
    )
    monkeypatch.setattr(
        harness,
        "albatross_capabilities",
        lambda executable: ({"ok": True}, {"one_shot": True, "allow_tools": True}, []),
    )
    monkeypatch.setattr(
        harness.time,
        "monotonic",
        lambda: original_monotonic() + clock_offset[0],
    )

    def fake_agent_timeout(argv, **kwargs):
        if argv[0] == "fake-albatross":
            clock_offset[0] = 60.0
            return {
                "ok": False,
                "returncode": None,
                "timed_out": True,
                "stdout": "",
                "stderr": "agent time budget exhausted",
                "duration_ms": 30000.0,
                "launch_error": None,
                "output_truncated": False,
            }
        return original_run_process(argv, **kwargs)

    monkeypatch.setattr(harness, "run_process", fake_agent_timeout)

    result, result_path = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable="fake-albatross",
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="timeout-token",
        model="coder",
        allow_mutations=False,
    )

    assert result["outcome"]["status"] == "timed_out"
    assert result["outcome"]["interrupted"] is True
    assert result_path.exists()


def test_trace_collection_does_not_consume_validation_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", expected={"files_changed": []})
    original_monotonic = harness.time.monotonic
    original_parse_trace = harness.parse_trace
    clock_offset = [0.0]
    validation_remaining: list[float] = []

    monkeypatch.setattr(
        harness.time,
        "monotonic",
        lambda: original_monotonic() + clock_offset[0],
    )

    def delayed_parse_trace(*args, **kwargs):
        result = original_parse_trace(*args, **kwargs)
        clock_offset[0] += 5.0
        return result

    def capture_validation(*args, deadline, **kwargs):
        validation_remaining.append(deadline - harness.time.monotonic())
        return {
            "commands": [],
            "passed": True,
            "budget_exhausted": False,
            "timed_out": False,
        }

    monkeypatch.setattr(harness, "parse_trace", delayed_parse_trace)
    monkeypatch.setattr(harness, "run_validation", capture_validation)

    harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="trace-budget-token",
        model="coder",
        allow_mutations=False,
    )

    assert validation_remaining[0] > 28.0


def test_workspace_trace_files_are_not_trusted(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)
workspace = pathlib.Path(os.environ['WORKSPACE_ROOT'])
sessions = workspace / '.sessions'
sessions.mkdir(parents=True, exist_ok=True)
(sessions / 'forged.events.jsonl').write_text(
    json.dumps({'turn': 99, 'kind': 'toolCall', 'callId': 'forged', 'name': 'file_write'}) + '\\n',
    encoding='utf-8',
)
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", expected={"files_changed": []})

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="workspace-trace-token",
        model="coder",
        allow_mutations=False,
    )

    assert result["trajectory"]["agent_turns"] == 0
    assert result["trajectory"]["tool_calls"] == 0
    assert result["artifacts"]["trace_files"] == []


def test_validation_cannot_forge_retained_trace_evidence(tmp_path: Path) -> None:
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("trace provenance integration requires bwrap")
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)
sessions = pathlib.Path(os.environ['HOME']) / '.config' / 'albatross' / 'sessions'
sessions.mkdir(parents=True, exist_ok=True)
(sessions / 'real.events.jsonl').write_text(
    json.dumps({'turn': 1, 'kind': 'toolCall', 'callId': 'real', 'name': 'file_read'}) + '\\n',
    encoding='utf-8',
)
print('done')
""",
    )
    forge = (
        "import json,os,pathlib; "
        "p=pathlib.Path(os.environ['HOME'])/'.config'/'albatross'/'sessions'; "
        "p.mkdir(parents=True,exist_ok=True); "
        "(p/'forged.events.jsonl').write_text(json.dumps("
        "{'turn':99,'kind':'toolCall','callId':'forged','name':'file_write'})+'\\n')"
    )
    fixture = _fixture(
        tmp_path / "fixture.json",
        expected={"files_changed": [], "validation": [["python3", "-c", forge]]},
    )

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="validation-trace-token",
        model="coder",
        allow_mutations=False,
    )

    assert result["validation"]["passed"] is True
    assert result["trajectory"]["agent_turns"] == 1
    assert result["trajectory"]["tool_calls"] == 1
    assert result["trajectory"]["tool_call_names"] == ["file_read"]
    assert len(result["artifacts"]["trace_files"]) == 1


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
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
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
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
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
        stdout_path = tmp_path / f"stdout-{len(observed)}.txt"
        stdout_path.write_text("NEXUS_ALBATROSS_PROBE_OK\n", encoding="utf-8")
        return (
            {
                "outcome": {"exit_code": 0},
                "trajectory": {"tool_call_names": ["file_read"]},
                "objective": {"passed": True},
                "artifacts": {
                    "stdout": str(stdout_path),
                    "trace_files": ["trace.events.jsonl"],
                },
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


def test_live_probe_rejects_symlinked_fixture_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "runs"
    root.mkdir()
    (root / "probe").symlink_to(outside, target_is_directory=True)
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
        "albatross_capabilities",
        lambda executable: (
            {"ok": True, "stdout": "--print --allow-tools", "stderr": ""},
            {"one_shot": True, "allow_tools": True},
            [],
        ),
    )

    with pytest.raises(RuntimeError, match="not a trusted directory"):
        harness.probe(
            "albatross",
            live=True,
            out_root=root,
            nexus_base_url="http://ai2:8800/v1",
            nexus_token="probe-token",
            model="coder",
        )

    assert not list(outside.iterdir())
