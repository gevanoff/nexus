from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_compare_review_fixes", path)
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


def _fixture(
    path: Path,
    *,
    files: dict[str, str],
    expected: dict | None = None,
    mission: object = "Inspect the repository and complete the requested task.",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "review-fix",
                "description": "review regression fixture",
                "repository": {"files": files},
                "mission": mission,
                "expected": {"files_changed": []} if expected is None else expected,
                "limits": {"wall_time_sec": 30, "max_agent_steps": 8},
                "tags": ["review-regression"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_fixture_requires_objective_or_validation_evidence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"}, expected={})

    with pytest.raises(ValueError, match="objective check or validation"):
        harness.load_fixture(fixture)


def test_fixture_rejects_mission_too_large_for_one_shot_argv(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path / "fixture.json",
        files={"app.py": "VALUE = 1\n"},
        mission="m" * (harness.MAX_MISSION_BYTES + 1),
    )

    with pytest.raises(ValueError, match=r"mission exceeds 64000 byte limit"):
        harness.load_fixture(fixture)


@pytest.mark.parametrize("content", [None, 7, {}, []])
def test_fixture_rejects_non_string_file_content(tmp_path: Path, content) -> None:
    fixture = _fixture(
        tmp_path / "fixture.json",
        files={"app.py": content},
    )

    with pytest.raises(ValueError, match=r"fixture file app\.py content must be a string"):
        harness.load_fixture(fixture)


def test_relative_albatross_path_is_resolved_before_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _write_executable(
        tmp_path / "bin" / "albatross",
        "#!/usr/bin/env python3\nprint('albatross 2.4.0')\n",
    )
    monkeypatch.chdir(tmp_path)

    version = harness.albatross_version("./bin/albatross")

    assert version["installed"] is True
    assert Path(version["executable"]).is_absolute()
    assert Path(version["executable"]) == binary.resolve()


def test_offline_binary_probes_do_not_require_linux_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
elif '--help' in sys.argv:
    print('albatross --print --allow-tools')
""",
    )
    monkeypatch.setattr(harness.sys, "platform", "linux")

    def unavailable_procfs(pid: int):
        raise OSError("restricted procfs")

    monkeypatch.setattr(harness, "_linux_direct_children", unavailable_procfs)

    version = harness.albatross_version(str(binary))
    help_result, capabilities, missing = harness.albatross_capabilities(str(binary))

    assert version["installed"] is True
    assert help_result["ok"] is True
    assert capabilities["one_shot"] is True
    assert capabilities["allow_tools"] is True
    assert missing == []


def test_offline_probe_fallback_reaps_process_group_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("process-group cleanup regression requires Linux")
    marker = tmp_path / "fallback-late-write.txt"
    child = (
        "import pathlib,time; "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    binary = _write_executable(
        tmp_path / "albatross",
        f"""#!/usr/bin/env python3
import subprocess
import sys
subprocess.Popen([sys.executable, '-c', {child!r}])
if '--version' in sys.argv:
    print('albatross 2.4.0')
elif '--help' in sys.argv:
    print('albatross --print --allow-tools')
""",
    )

    def unavailable_procfs(pid: int):
        raise OSError("restricted procfs")

    monkeypatch.setattr(harness, "_linux_direct_children", unavailable_procfs)

    version = harness.albatross_version(str(binary))
    help_result, _, missing = harness.albatross_capabilities(str(binary))
    time.sleep(0.7)

    assert version["installed"] is True
    assert help_result["ok"] is True
    assert missing == []
    assert not marker.exists()


def test_linux_binary_probes_contain_detached_descendants(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("subreaper containment is Linux-only")
    try:
        harness._linux_direct_children(os.getpid())
    except OSError:
        pytest.skip("procfs child enumeration is unavailable")
    marker = tmp_path / "late-probe-write.txt"
    child = (
        "import pathlib,time; "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    binary = _write_executable(
        tmp_path / "albatross",
        f"""#!/usr/bin/env python3
import subprocess
import sys
subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)
if '--version' in sys.argv:
    print('albatross 2.4.0')
elif '--help' in sys.argv:
    print('albatross --print --allow-tools')
""",
    )

    version = harness.albatross_version(str(binary))
    help_result, _, missing = harness.albatross_capabilities(str(binary))
    time.sleep(0.7)

    assert version["installed"] is True
    assert help_result["ok"] is True
    assert missing == []
    assert not marker.exists()


def test_linux_direct_children_includes_subprocesses_from_worker_threads() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("procfs child enumeration requires Linux")
    started = harness.threading.Event()
    release = harness.threading.Event()
    holder: dict[str, harness.subprocess.Popen] = {}

    def launch_from_worker() -> None:
        child = harness.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        holder["child"] = child
        started.set()
        release.wait(timeout=10.0)
        child.terminate()
        child.wait(timeout=5.0)

    worker = harness.threading.Thread(target=launch_from_worker)
    worker.start()
    assert started.wait(timeout=5.0)
    try:
        assert holder["child"].pid in harness._linux_direct_children(os.getpid())
    finally:
        release.set()
        worker.join(timeout=10.0)
        child = holder.get("child")
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5.0)
    assert not worker.is_alive()


def test_linux_direct_children_fails_closed_for_live_task_without_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_root = tmp_path / "task"
    (task_root / "123").mkdir(parents=True)
    real_scandir = os.scandir
    monkeypatch.setattr(harness.os, "scandir", lambda path: real_scandir(task_root))

    with pytest.raises(FileNotFoundError):
        harness._linux_direct_children(456)


def test_run_rejects_binary_missing_required_capability_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    fake = _write_executable(
        tmp_path / "albatross",
        f"""#!/usr/bin/env python3
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print')
    raise SystemExit(0)
pathlib.Path({str(marker)!r}).write_text('executed', encoding='utf-8')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"})

    with pytest.raises(RuntimeError, match=r"missing required capabilities: allow_tools"):
        harness.run_albatross_fixture(
            fixture,
            out_root=tmp_path / "runs",
            executable=str(fake),
            nexus_base_url="http://ai2:8800/v1",
            nexus_token="review-token",
            model="coder",
            allow_mutations=False,
        )

    assert not marker.exists()


def test_read_only_env_removes_mutating_tool_surfaces(tmp_path: Path) -> None:
    env = harness.build_albatross_env(
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="review-token",
        model="coder",
        workspace=tmp_path / "work",
        home=tmp_path / "home",
        temp_dir=tmp_path / "tmp",
        max_steps=8,
        allow_mutations=False,
    )
    tools = set(env["AGENT_TOOLS"].split(","))

    assert "file_read" in tools
    assert tools.isdisjoint({"apply_patch", "file_write", "file_edit", "run_tests", "update_plan", "shell"})


def test_mutating_env_excludes_unsandboxed_test_execution(tmp_path: Path) -> None:
    env = harness.build_albatross_env(
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="review-token",
        model="coder",
        workspace=tmp_path / "work",
        home=tmp_path / "home",
        temp_dir=tmp_path / "tmp",
        max_steps=8,
        allow_mutations=True,
    )
    tools = set(env["AGENT_TOOLS"].split(","))

    assert "file_edit" in tools
    assert "run_tests" not in tools
    assert "shell" not in tools


def test_read_only_run_auto_approves_its_restricted_tool_surface(tmp_path: Path) -> None:
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
if '--allow-tools' not in sys.argv:
    print('missing non-interactive tool approval', file=sys.stderr)
    raise SystemExit(9)
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"})

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="review-token",
        model="coder",
        allow_mutations=False,
    )

    assert result["outcome"]["completed"] is True


def test_result_redacts_truncated_process_output_when_token_is_in_scope(tmp_path: Path) -> None:
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
print('discarded-prefix-' + ('x' * 100_256))
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"})

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="review-token",
        model="coder",
        allow_mutations=False,
    )

    stdout = Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8")
    assert result["outcome"]["completed"] is True
    assert result["artifacts"]["process_output_truncated"] is True
    assert stdout == "(redacted)"
    assert "discarded-prefix" not in stdout


def test_parse_trace_streams_without_path_read_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    trace = session_root / "one.events.jsonl"
    trace.write_text(
        json.dumps({"turn": 1, "kind": "toolCall", "name": "file_read"}) + "\n"
        + json.dumps({"turn": 1, "kind": "turnSummary", "steps": 2}) + "\n",
        encoding="utf-8",
    )

    def forbidden_read_text(self: Path, *args, **kwargs):
        raise AssertionError(f"parse_trace must stream instead of read_text: {self}")

    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    result = harness.parse_trace(session_root)

    assert result["tool_calls"] == ["file_read"]
    assert result["agent_steps"] == 2


def test_parse_trace_counts_non_object_json_as_malformed(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    trace = session_root / "one.events.jsonl"
    trace.write_text(
        "null\n[]\n" + json.dumps({"turn": 1, "kind": "turnSummary", "steps": 1}) + "\n",
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root)

    assert result["malformed_trace_lines"] == 2
    assert result["agent_steps"] == 1


def test_parse_trace_counts_oversized_numeric_step_string_as_malformed(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    (session_root / "one.events.jsonl").write_text(
        json.dumps({"turn": 1, "kind": "turnSummary", "steps": "7" * 5_000}) + "\n",
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root)

    assert result["agent_steps"] == 0
    assert result["malformed_trace_lines"] == 1


def test_parse_trace_counts_oversized_integer_steps_as_malformed(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    (session_root / "one.events.jsonl").write_text(
        json.dumps({"turn": 1, "kind": "turnSummary", "steps": 10 ** 1_000}) + "\n",
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root)

    assert result["agent_steps"] == 0
    assert result["malformed_trace_lines"] == 1


def test_parse_trace_counts_same_turn_number_in_distinct_trace_files(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    event = json.dumps({"turn": 1, "kind": "turnSummary", "steps": 1}) + "\n"
    (session_root / "first.events.jsonl").write_text(event, encoding="utf-8")
    (session_root / "second.events.jsonl").write_text(event, encoding="utf-8")

    result = harness.parse_trace(session_root)

    assert result["agent_turns"] == 2
    assert result["agent_steps"] == 2


def test_parse_trace_counts_only_one_summary_per_trace_turn(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    records = [
        {"turn": 1, "kind": "turnSummary", "steps": 80}
        for _ in range(20)
    ]
    (session_root / "one.events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root)

    assert result["agent_turns"] == 1
    assert result["agent_steps"] == 80
    assert result["malformed_trace_lines"] == 19


def test_parse_trace_bounds_steps_across_distinct_turns(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    records = [
        {"turn": turn, "kind": "turnSummary", "steps": 1}
        for turn in range(20)
    ]
    (session_root / "one.events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root, max_agent_steps=8)

    assert result["agent_turns"] == 20
    assert result["agent_steps"] == 8
    assert result["malformed_trace_lines"] == 12


def test_parse_trace_rejects_boolean_turn_identifiers(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    records = [
        {"turn": True, "kind": "toolCall", "name": "file_read"},
        {"turn": False, "kind": "toolCall", "name": "grep"},
        {"turn": 1, "kind": "turnSummary", "steps": 1},
    ]
    (session_root / "one.events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root)

    assert result["agent_turns"] == 1
    assert result["agent_steps"] == 1
    assert result["tool_calls"] == []
    assert result["malformed_trace_lines"] == 2


def test_parse_trace_requires_bounded_integer_turns_for_all_metrics(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    records = [
        {"kind": "toolCall", "name": "missing"},
        {"turn": "forged", "kind": "toolCall", "name": "string"},
        {"turn": 1.5, "kind": "contextCompacted"},
        {"turn": -1, "kind": "turnSummary", "steps": 9},
        {"turn": 10 ** 100, "kind": "toolCall", "name": "huge"},
        {"turn": 2, "kind": "toolCall", "name": "file_read"},
    ]
    (session_root / "one.events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    result = harness.parse_trace(session_root)

    assert result["agent_turns"] == 1
    assert result["agent_steps"] == 0
    assert result["tool_calls"] == ["file_read"]
    assert result["context_resets"] == 0
    assert result["malformed_trace_lines"] == 5


def test_parse_trace_records_source_open_failure_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    unreadable = session_root / "a.events.jsonl"
    readable = session_root / "b.events.jsonl"
    unreadable.write_text(json.dumps({"turn": 1, "kind": "toolCall", "name": "grep"}) + "\n")
    readable.write_text(json.dumps({"turn": 2, "kind": "toolCall", "name": "file_read"}) + "\n")
    original_open = Path.open

    def fail_one_open(self: Path, *args, **kwargs):
        if self == unreadable:
            raise PermissionError("review regression")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_one_open)
    result = harness.parse_trace(session_root, artifact_dir=tmp_path / "artifacts")

    assert result["tool_calls"] == ["file_read"]
    assert len(result["trace_files"]) == 1
    assert len(result["trace_omissions"]) == 1
    assert "could not read trace" in result["trace_omissions"][0]["reason"]


def test_application_events_jsonl_is_not_counted_as_albatross_trace(tmp_path: Path) -> None:
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
    fixture = _fixture(
        tmp_path / "fixture.json",
        files={
            "app.events.jsonl": json.dumps(
                {
                    "turn": 99,
                    "kind": "toolCall",
                    "callId": "fake",
                    "name": "shell",
                    "args": {},
                    "depth": 0,
                }
            )
            + "\n",
            "app.py": "VALUE = 1\n",
        },
    )

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="review-token",
        model="coder",
        allow_mutations=False,
    )

    assert result["outcome"]["completed"] is True
    assert result["trajectory"]["tool_call_names"] == []
    assert result["trajectory"]["agent_turns"] == 0
    assert result["artifacts"]["trace_files"] == []


def test_probe_fails_when_required_cli_surface_is_missing(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print')
    raise SystemExit(0)
raise SystemExit(0)
""",
    )

    report = harness.probe(str(fake))

    assert report["ok"] is False
    assert report["capabilities"]["one_shot"] is True
    assert report["capabilities"]["allow_tools"] is False
    assert "allow_tools" in report["compatibility"]["missing"]


def test_workspace_snapshot_redacts_secret_from_git_failure_label_and_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    secret = "nexus-git-failure-secret"
    (workspace / "evidence.txt").write_text("content\n", encoding="utf-8")
    original_git = harness.git

    def fail_no_index(argv, **kwargs):
        if "--no-index" in argv:
            return {
                "ok": False,
                "returncode": 1,
                "timed_out": False,
                "stdout": "",
                "stderr": f"fatal: could not diff evidence-{secret}.txt",
                "duration_ms": 0.0,
                "launch_error": None,
                "output_truncated": True,
            }
        return original_git(argv, **kwargs)

    monkeypatch.setattr(harness, "git", fail_no_index)

    with pytest.raises(RuntimeError) as raised:
        harness.workspace_snapshot(workspace, baseline, artifacts, secrets=[secret])

    assert secret not in str(raised.value)
    assert "(redacted)" in str(raised.value)


def test_whitespace_split_raw_secret_is_redacted_from_retained_evidence(
    tmp_path: Path,
) -> None:
    secret = "nexus-whitespace-split-raw-secret"
    split_secret = " \n\t".join(secret)
    evidence = f"prefix:{split_secret}:suffix"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    retained = artifacts / "final.txt"
    retained.write_text(evidence, encoding="utf-8")

    assert harness.redact_text(evidence, [secret]) == "(redacted)"
    assert harness.redact_value({"message": evidence}, [secret]) == {
        "message": "(redacted)"
    }
    assert harness.scrub_retained_artifacts(artifacts, [secret]) == ["final.txt"]
    assert not retained.exists()


def test_whitespace_split_hex_secret_is_redacted_from_retained_evidence(
    tmp_path: Path,
) -> None:
    secret = "nexus-whitespace-split-hex-secret"
    hexadecimal = secret.encode("utf-8").hex()
    split_hexadecimal = " \n\t".join(hexadecimal)
    evidence = f"prefix:{split_hexadecimal}:suffix"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    retained = artifacts / "hex.txt"
    retained.write_text(evidence, encoding="utf-8")

    assert harness.redact_text(evidence, [secret]) == "(redacted)"
    assert harness.scrub_retained_artifacts(artifacts, [secret]) == ["hex.txt"]
    assert not retained.exists()


def test_case_folded_raw_hex_secret_is_redacted_from_retained_evidence(
    tmp_path: Path,
) -> None:
    secret = "0123456789abcdef" * 4
    evidence = f"prefix:{secret.upper()}:suffix"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    retained = artifacts / "uppercase-token.txt"
    retained.write_text(evidence, encoding="utf-8")

    assert harness.redact_text(evidence, [secret]) == "(redacted)"
    assert harness.scrub_retained_artifacts(artifacts, [secret]) == [
        "uppercase-token.txt"
    ]
    assert not retained.exists()


@pytest.mark.parametrize("encoded", [False, True])
def test_secret_paths_are_detected_across_component_boundaries(encoded: bool) -> None:
    secret = "nexus-path-component-secret-value"
    value = (
        harness.base64.urlsafe_b64encode(secret.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
        if encoded
        else secret
    )
    midpoint = len(value) // 2

    assert harness._contains_secret_path(
        f"prefix/{value[:midpoint]}/{value[midpoint:]}/suffix", [secret]
    )


def test_workspace_snapshot_omits_path_reconstructing_raw_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    secret = "nexus-nested-path-secret"
    midpoint = len(secret) // 2
    protected = workspace / secret[:midpoint] / secret[midpoint:]
    protected.parent.mkdir()
    protected.write_text("safe content\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(
        workspace, baseline, artifacts, secrets=[secret]
    )

    assert snapshot["files_changed"] == ["(redacted)"]
    assert snapshot["evidence_omissions"]
    assert not (artifacts / "final-files" / secret[:midpoint]).exists()


def test_workspace_snapshot_neutralizes_worktree_ident_attributes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {
            "repository": {
                "files": {
                    ".gitattributes": "*.txt ident\n",
                    "value.txt": "$Id$\n",
                }
            }
        },
    )
    (workspace / "value.txt").write_text("$Id: forged $\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert snapshot["files_changed"] == ["value.txt"]
    assert "+$Id: forged $" in (artifacts / "final.diff").read_text(
        encoding="utf-8"
    )
    assert (artifacts / "final-files" / "value.txt").read_text(
        encoding="utf-8"
    ) == "$Id: forged $\n"


def test_initialize_workspace_neutralizes_fixture_ident_attributes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {
            "repository": {
                "files": {
                    ".gitattributes": "*.txt ident\n",
                    "value.txt": "$Id: forged $\n",
                }
            }
        },
    )

    stored = harness.git(["show", f"{baseline}:value.txt"], cwd=workspace)
    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert stored["ok"] is True
    assert stored["stdout"] == "$Id: forged $\n"
    assert snapshot["files_changed"] == []


def test_validation_commands_share_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = {"expected": {"validation": [["first"], ["second"]]}}
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    observed_timeouts: list[float] = []
    monotonic_values = iter([9.0, 9.4])

    monkeypatch.setattr(harness.time, "monotonic", lambda: next(monotonic_values))

    def fake_run_process(argv, *, cwd, env=None, timeout_sec=60.0, secrets=(), isolate_process_group=False, **kwargs):
        observed_timeouts.append(timeout_sec)
        assert isolate_process_group is True
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0.0,
            "launch_error": None,
        }

    monkeypatch.setattr(harness, "run_process", fake_run_process)
    monkeypatch.setattr(
        harness,
        "_validation_sandbox_argv",
        lambda argv, workspace, home, temp_dir: (argv, {}),
    )
    result = harness.run_validation(
        fixture,
        workspace,
        home,
        temp_dir,
        deadline=10.0,
    )

    assert result["passed"] is True
    assert observed_timeouts == pytest.approx([1.0, 0.6])


def test_validation_command_can_use_more_than_300_seconds_of_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    observed_timeouts: list[float] = []
    monkeypatch.setattr(harness.time, "monotonic", lambda: 100.0)

    def fake_run_process(argv, *, cwd, env=None, timeout_sec=60.0, secrets=(), isolate_process_group=False, **kwargs):
        observed_timeouts.append(timeout_sec)
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0.0,
            "launch_error": None,
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
        deadline=700.0,
    )

    assert result["passed"] is True
    assert observed_timeouts == pytest.approx([600.0])


def test_validation_cannot_modify_measured_workspace(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("validation integration requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    marker = workspace / "validation-write.txt"
    code = (
        "from pathlib import Path\n"
        "marker = Path('validation-write.txt')\n"
        "try:\n"
        "    marker.write_text('forged', encoding='utf-8')\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('validation workspace was writable')\n"
    )

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", code]]}},
        workspace,
        home,
        temp_dir,
        deadline=time.monotonic() + 10.0,
    )

    assert result["passed"] is True
    assert not marker.exists()


def test_validation_scratch_entry_limit_fails_the_command(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("validation integration requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    code = (
        "from pathlib import Path\n"
        "root = Path('/tmp/entries')\n"
        "root.mkdir()\n"
        f"for index in range({harness.MAX_VALIDATION_SCRATCH_ENTRIES + 8}):\n"
        "    (root / str(index)).touch()\n"
    )

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", code]]}},
        workspace,
        home,
        temp_dir,
        deadline=time.monotonic() + 10.0,
    )

    assert result["passed"] is False
    assert (
        result["commands"][0]["launch_error"] == "scratch_limit_exceeded"
    ), result["commands"][0]["stderr"]
    assert "filesystem quota" in result["commands"][0]["stderr"]


def test_scratch_scan_stops_without_materializing_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for index in range(8):
        (scratch / str(index)).touch()
    real_scandir = os.scandir
    consumed = 0

    class GuardedScandir:
        def __init__(self, path: Path) -> None:
            self.inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            consumed += 1
            if consumed > 4:
                raise AssertionError("scratch scan consumed entries beyond its limit")
            return next(self.inner)

    monkeypatch.setattr(harness.os, "scandir", GuardedScandir)

    error = harness._scratch_limit_error(scratch, max_bytes=1024, max_entries=3)

    assert error == "validation scratch exceeded 3 entry limit"
    assert consumed == 4


def test_validation_file_size_limit_is_enforced(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("validation integration requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    code = (
        "from pathlib import Path\n"
        "try:\n"
        f"    Path('/tmp/large').write_bytes(b'x' * {harness.MAX_VALIDATION_FILE_BYTES + 1})\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('validation file limit was not enforced')\n"
    )

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", code]]}},
        workspace,
        home,
        temp_dir,
        deadline=time.monotonic() + 10.0,
    )

    assert result["passed"] is True


def test_validation_process_and_memory_limits_are_inherited(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("validation integration requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    code = (
        "import mmap\n"
        "import resource\n"
        f"assert resource.getrlimit(resource.RLIMIT_NPROC) == ({harness.MAX_VALIDATION_PROCESSES}, {harness.MAX_VALIDATION_PROCESSES})\n"
        f"assert resource.getrlimit(resource.RLIMIT_AS) == ({harness.MAX_VALIDATION_MEMORY_BYTES}, {harness.MAX_VALIDATION_MEMORY_BYTES})\n"
        "reservation = mmap.mmap(-1, 4 * 1024 * 1024 * 1024)\n"
        "reservation.close()\n"
        "try:\n"
        f"    bytearray({harness.MAX_VALIDATION_MEMORY_BYTES + 1})\n"
        "except MemoryError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('validation memory limit was not enforced')\n"
    )

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", code]]}},
        workspace,
        home,
        temp_dir,
        deadline=time.monotonic() + 10.0,
    )

    assert result["passed"] is True


def test_validation_enforces_aggregate_resident_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("validation integration requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    monkeypatch.setattr(harness, "MAX_VALIDATION_AGGREGATE_MEMORY_BYTES", 1)

    result = harness.run_validation(
        {
            "expected": {
                "validation": [["python3", "-c", "import time; time.sleep(2)"]]
            }
        },
        workspace,
        home,
        temp_dir,
        deadline=time.monotonic() + 10.0,
    )

    assert result["passed"] is False
    assert result["commands"][0]["launch_error"] == "validation_memory_limit_exceeded"
    assert "process tree" in result["commands"][0]["stderr"]


def test_validation_accounts_for_deleted_open_scratch_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("validation integration requires Linux")
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    for path in (workspace, home, temp_dir):
        path.mkdir()
    monkeypatch.setattr(harness, "MAX_VALIDATION_SCRATCH_BYTES", 1024)
    code = (
        "import os,time\n"
        "descriptor = os.open('/tmp/deleted', os.O_CREAT | os.O_WRONLY, 0o600)\n"
        "os.unlink('/tmp/deleted')\n"
        "remaining = 8192\n"
        "while remaining:\n"
        "    remaining -= os.write(descriptor, b'x' * remaining)\n"
        "os.fsync(descriptor)\n"
        "time.sleep(2)\n"
    )

    result = harness.run_validation(
        {"expected": {"validation": [["python3", "-c", code]]}},
        workspace,
        home,
        temp_dir,
        deadline=time.monotonic() + 10.0,
    )

    assert result["passed"] is False
    assert result["commands"][0]["launch_error"] == "scratch_limit_exceeded"
    assert "filesystem quota" in result["commands"][0]["stderr"]


def test_missing_validation_executable_is_failed_and_execution_state_is_discarded(tmp_path: Path) -> None:
    if harness.shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
        pytest.skip("validation integration requires bwrap")
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import os
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)
root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
(root / 'leak.txt').write_text(os.environ['OPENAI_API_KEY'], encoding='utf-8')
print('done')
""",
    )
    fixture = _fixture(
        tmp_path / "fixture.json",
        files={"app.py": "VALUE = 1\n"},
        expected={"validation": [["definitely-not-a-real-validation-command-xyz"]]},
    )
    token = "nexus-validation-launch-secret"

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
        allow_mutations=True,
    )

    run_root = Path(result["artifacts"]["run_root"])
    assert result["outcome"]["completed"] is False
    assert result["validation"]["passed"] is False
    assert result["validation"]["commands"][0]["returncode"] != 0
    assert result["validation"]["commands"][0]["launch_error"] is None
    assert not (run_root / "workspace").exists()
    assert not (run_root / "home").exists()
    assert not (run_root / "tmp").exists()
    for path in (run_root / "artifacts").rglob("*"):
        if path.is_file():
            assert token not in path.read_text(encoding="utf-8", errors="ignore")


def test_discard_repairs_restrictive_permissions_and_verifies_absence(tmp_path: Path) -> None:
    target = tmp_path / "unsafe"
    nested = target / "nested"
    nested.mkdir(parents=True)
    (nested / "secret.txt").write_text("secret", encoding="utf-8")
    nested.chmod(0)

    harness.discard_path_verified(target)

    assert not os.path.lexists(target)


def test_discard_does_not_chmod_external_hard_link_target(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode regression")
    outside = tmp_path / "outside-executable"
    outside.write_text("external\n", encoding="utf-8")
    outside.chmod(0o751)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    target = tmp_path / "unsafe"
    nested = target / "nested"
    nested.mkdir(parents=True)
    os.link(outside, nested / "linked-file")
    nested.chmod(0)

    harness.discard_path_verified(target)

    assert not os.path.lexists(target)
    assert outside.read_text(encoding="utf-8") == "external\n"
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode


def test_keyboard_interrupt_discards_fixture_execution_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"})
    token = "nexus-interrupted-run-secret"

    monkeypatch.setattr(
        harness,
        "albatross_version",
        lambda executable: {
            "installed": True,
            "executable": executable,
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

    def initialize_with_secret(workspace: Path, fixture_data: dict) -> str:
        workspace.mkdir(parents=True)
        (workspace / "secret.txt").write_text(token, encoding="utf-8")
        return "baseline"

    def interrupt_run(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(harness, "initialize_workspace", initialize_with_secret)
    monkeypatch.setattr(harness, "run_process", interrupt_run)
    out_root = tmp_path / "runs"

    with pytest.raises(KeyboardInterrupt):
        harness.run_albatross_fixture(
            fixture,
            out_root=out_root,
            executable="albatross",
            nexus_base_url="http://ai2:8800/v1",
            nexus_token=token,
            model="coder",
        )

    assert not list(out_root.rglob("albatross"))
    assert not list(out_root.rglob("secret.txt"))


def test_isolated_process_group_terminates_background_descendants(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("subreaper containment is Linux-only")
    marker = tmp_path / "late-write.txt"
    child = (
        "import pathlib,time; "
        "time.sleep(0.35); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True); "
        "print('parent complete')"
    )

    result = harness.run_process(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        timeout_sec=2.0,
        isolate_process_group=True,
    )
    time.sleep(0.5)

    if result["launch_error"] == "subreaper_unavailable":
        assert not marker.exists()
        return
    assert result["ok"] is True
    assert not marker.exists()


def test_isolated_process_execution_fails_closed_off_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness.sys, "platform", "darwin")

    result = harness.run_process(
        [sys.executable, "-c", "raise SystemExit(0)"],
        cwd=tmp_path,
        isolate_process_group=True,
    )

    assert result["ok"] is False
    assert result["launch_error"] == "process_group_unsupported"
    assert "Linux host" in result["stderr"]


def test_isolated_process_execution_fails_closed_when_descendants_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("subreaper containment is Linux-only")

    def fail_child_inspection(pid: int) -> set[int]:
        raise PermissionError("review regression")

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("the child must not launch without descendant inspection")

    monkeypatch.setattr(harness, "_linux_direct_children", fail_child_inspection)
    monkeypatch.setattr(harness.subprocess, "Popen", forbidden_popen)

    result = harness.run_process(
        [sys.executable, "-c", "raise SystemExit(0)"],
        cwd=tmp_path,
        isolate_process_group=True,
    )

    assert result["ok"] is False
    assert result["launch_error"] == "subreaper_unavailable"
    assert "could not enable descendant containment" in result["stderr"]


def test_isolated_process_raises_when_post_launch_containment_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("subreaper containment is Linux-only")
    try:
        harness._linux_direct_children(os.getpid())
    except OSError:
        pytest.skip("procfs child enumeration is unavailable")

    def fail_containment(baseline_children: set[int]) -> None:
        raise RuntimeError("review regression")

    monkeypatch.setattr(harness, "_terminate_linux_adopted_children", fail_containment)

    with pytest.raises(RuntimeError, match="SECURITY: could not verify descendant containment"):
        harness.run_process(
            [sys.executable, "-c", "raise SystemExit(0)"],
            cwd=tmp_path,
            isolate_process_group=True,
        )


def test_isolated_process_reaps_detached_descendant_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("subreaper containment is Linux-only")
    try:
        harness._linux_direct_children(os.getpid())
    except OSError:
        pytest.skip("procfs child enumeration is unavailable")

    marker = tmp_path / "interrupted-late-write.txt"
    child = (
        "import pathlib,time; "
        "time.sleep(1.2); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True); "
        "time.sleep(5)"
    )
    original_popen = harness.subprocess.Popen

    class InterruptingProcess:
        def __init__(self, *args, **kwargs):
            self._process = original_popen(*args, **kwargs)
            self._first_wait = True

        def __getattr__(self, name):
            return getattr(self._process, name)

        def wait(self, timeout=None):
            if self._first_wait:
                self._first_wait = False
                time.sleep(0.2)
                raise KeyboardInterrupt
            return self._process.wait(timeout=timeout)

    monkeypatch.setattr(harness.subprocess, "Popen", InterruptingProcess)

    with pytest.raises(KeyboardInterrupt):
        harness.run_process(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            timeout_sec=2.0,
            isolate_process_group=True,
        )
    time.sleep(1.4)

    assert not marker.exists()


def test_isolated_process_contains_child_when_popen_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("subreaper containment is Linux-only")
    try:
        harness._linux_direct_children(os.getpid())
    except OSError:
        pytest.skip("procfs child enumeration is unavailable")

    marker = tmp_path / "interrupted-launch-late-write.txt"
    child = (
        "import pathlib,time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    original_popen = harness.subprocess.Popen
    original_subreaper_state = harness._linux_subreaper_enabled()

    def interrupt_after_launch(*args, **kwargs):
        original_popen(
            [sys.executable, "-c", child],
            cwd=kwargs.get("cwd"),
            stdout=harness.subprocess.DEVNULL,
            stderr=harness.subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        raise KeyboardInterrupt

    monkeypatch.setattr(harness.subprocess, "Popen", interrupt_after_launch)

    with pytest.raises(KeyboardInterrupt):
        harness.run_process(
            [sys.executable, "-c", "raise SystemExit(0)"],
            cwd=tmp_path,
            isolate_process_group=True,
        )
    time.sleep(1.2)

    assert not marker.exists()
    assert harness._linux_subreaper_enabled() is original_subreaper_state


def test_large_trace_is_sanitized_and_raw_execution_tree_is_not_retained(tmp_path: Path) -> None:
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
root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
home = pathlib.Path(os.environ['HOME'])
sessions = home / '.config' / 'albatross' / 'sessions'
sessions.mkdir(parents=True, exist_ok=True)
payload = 'x' * 2200000 + os.environ['OPENAI_API_KEY']
(sessions / 'large.events.jsonl').write_text(json.dumps({'turn': 1, 'kind': 'toolCall', 'name': 'file_read', 'args': {'payload': payload}}) + '\\n', encoding='utf-8')
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"})
    token = "nexus-large-trace-secret"

    result, _ = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
        allow_mutations=False,
    )

    run_root = Path(result["artifacts"]["run_root"])
    trace_path = Path(result["artifacts"]["trace_files"][0])
    assert trace_path.stat().st_size > 2_000_000
    assert token not in trace_path.read_text(encoding="utf-8")
    assert not (run_root / "workspace").exists()
    assert not (run_root / "home").exists()
    assert not (run_root / "tmp").exists()
    assert result["workspace"]["execution_workspace_retained"] is False


def test_list_fixtures_loads_each_fixture_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    calls: list[Path] = []

    def fake_load(path: Path):
        calls.append(path)
        return {"id": path.stem, "description": f"fixture {path.stem}"}

    monkeypatch.setattr(harness, "bundled_fixture_paths", lambda: paths)
    monkeypatch.setattr(harness, "load_fixture", fake_load)
    monkeypatch.setattr(sys, "argv", ["coding_harness_compare.py", "list-fixtures", "--json"])

    assert harness.main() == 0
    assert calls == paths
    assert json.loads(capsys.readouterr().out)[0]["id"] == "a"


def test_retained_run_discards_execution_tree_scrubs_secret_and_hashes_final_diff(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys

if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --allow-tools')
    raise SystemExit(0)

root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
secret = os.environ['OPENAI_API_KEY']
(root / 'leak.txt').write_text(secret + '\\n', encoding='utf-8')
subprocess.run(['git', 'add', 'leak.txt'], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
subprocess.run(['git', 'commit', '-m', 'capture secret'], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
sessions = pathlib.Path(os.environ['HOME']) / '.config' / 'albatross' / 'sessions'
sessions.mkdir(parents=True, exist_ok=True)
(sessions / 'fake.events.jsonl').write_text(
    json.dumps({'turn': 1, 'kind': 'toolCall', 'callId': '1', 'name': 'file_write', 'args': {'path': 'leak.txt'}, 'depth': 0}) + '\\n',
    encoding='utf-8',
)
print('done')
""",
    )
    fixture = _fixture(
        tmp_path / "fixture.json",
        files={"app.py": "VALUE = 1\n"},
        expected={"allowed_files_changed": ["leak.txt"]},
    )
    token = "nexus-review-secret-token"

    result, result_path = harness.run_albatross_fixture(
        fixture,
        out_root=tmp_path / "runs",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
        allow_mutations=True,
    )

    run_root = Path(result["artifacts"]["run_root"])
    diff_path = Path(result["artifacts"]["diff"])
    assert result["outcome"]["completed"] is True
    assert result["workspace"]["git_metadata_retained"] is False
    assert result["workspace"]["execution_workspace_retained"] is False
    assert not (run_root / "workspace").exists()
    assert not (run_root / "home").exists()
    assert not (run_root / "tmp").exists()
    assert result_path.exists()
    assert result["workspace"]["diff_sha256"] == hashlib.sha256(diff_path.read_bytes()).hexdigest()

    for path in (run_root / "artifacts").rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert token not in text, f"secret remained in retained artifact: {path}"


def test_fixture_rejects_and_discards_unexpected_execution_root_entries(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
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
(workspace.parent / 'unexpected.txt').write_text(os.environ['OPENAI_API_KEY'], encoding='utf-8')
print('done')
""",
    )
    fixture = _fixture(tmp_path / "fixture.json", files={"app.py": "VALUE = 1\n"})
    out_root = tmp_path / "runs"
    token = "nexus-unexpected-root-secret"

    with pytest.raises(RuntimeError, match="unexpected entries remained") as raised:
        harness.run_albatross_fixture(
            fixture,
            out_root=out_root,
            executable=str(fake),
            nexus_base_url="http://ai2:8800/v1",
            nexus_token=token,
            model="coder",
        )

    assert token not in str(raised.value)
    assert not list(out_root.rglob("unexpected.txt"))
    assert not any(path.is_dir() for path in out_root.iterdir())


def test_discard_top_level_hard_link_does_not_chmod_external_inode(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX hard-link regression")
    outside = tmp_path / "outside-file"
    outside.write_text("external\n", encoding="utf-8")
    outside.chmod(0o640)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    linked_root = tmp_path / "execution-root"
    os.link(outside, linked_root)

    harness.discard_path_verified(linked_root)

    assert not os.path.lexists(linked_root)
    assert outside.read_text(encoding="utf-8") == "external\n"
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode


def test_workspace_snapshot_ignores_mutable_git_excludes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    hidden = "agent-hidden-output.txt"
    with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(f"{hidden}\n")
    (workspace / hidden).write_text("created\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert snapshot["dirty"] is True
    assert snapshot["files_changed"] == [hidden]
    assert hidden in (artifacts / "final.diff").read_text(encoding="utf-8")
    assert (artifacts / "final-files" / hidden).read_text(encoding="utf-8") == "created\n"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_workspace_snapshot_ignores_mutable_index_flags(tmp_path: Path, flag: str) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    assert harness.git(["update-index", flag, "app.py"], cwd=workspace)["ok"] is True
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    snapshot = harness.workspace_snapshot(workspace, baseline, artifacts)

    assert snapshot["dirty"] is True
    assert snapshot["files_changed"] == ["app.py"]
    assert "+VALUE = 2" in (artifacts / "final.diff").read_text(encoding="utf-8")
    assert (artifacts / "final-files" / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_workspace_snapshot_bounds_aggregate_hard_link_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("hard-link aggregate regression requires POSIX")
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    first = workspace / "first.txt"
    first.write_text("12345", encoding="utf-8")
    os.link(first, workspace / "second.txt")
    monkeypatch.setattr(harness, "MAX_SNAPSHOT_FILE_BYTES", 8)

    with pytest.raises(RuntimeError, match="byte file-evidence limit"):
        harness.workspace_snapshot(workspace, baseline, artifacts)


def test_workspace_snapshot_bounds_changed_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    for index in range(3):
        (workspace / f"output-{index}.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(harness, "MAX_SNAPSHOT_CHANGED_FILES", 2)

    with pytest.raises(RuntimeError, match="changed-file limit"):
        harness.workspace_snapshot(workspace, baseline, artifacts)


def test_workspace_snapshot_enforces_time_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    baseline = harness.initialize_workspace(
        workspace,
        {"repository": {"files": {"app.py": "VALUE = 1\n"}}},
    )
    monkeypatch.setattr(harness, "MAX_SNAPSHOT_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="snapshot time budget exhausted"):
        harness.workspace_snapshot(workspace, baseline, tmp_path / "artifacts")


def test_run_process_redacts_secret_before_tail_truncation(tmp_path: Path) -> None:
    secret = "nexus-boundary-secret-value"
    output_limit = 64
    filler = "x" * (output_limit + 1 - len(secret))

    result = harness.run_process(
        [sys.executable, "-c", f"import sys; sys.stdout.write({(secret + filler)!r})"],
        cwd=tmp_path,
        secrets=[secret],
        output_limit_chars=output_limit,
    )

    assert result["ok"] is True
    assert result["output_truncated"] is True
    assert secret not in result["stdout"]
    assert secret[1:] not in result["stdout"]
    assert len(result["stdout"]) <= output_limit


def test_run_process_retains_overlap_for_longest_secret_encoding(tmp_path: Path) -> None:
    secret = "nexus-encoded-boundary-secret"
    encoded = secret.encode("utf-8").hex()
    output_limit = 64
    output = ("p" * 200) + encoded + ("x" * 50)

    result = harness.run_process(
        [sys.executable, "-c", f"import sys; sys.stdout.write({output!r})"],
        cwd=tmp_path,
        secrets=[secret],
        output_limit_chars=output_limit,
    )

    assert result["ok"] is True
    assert result["output_truncated"] is True
    assert result["stdout"] == "(redacted)"
    assert encoded[-14:] not in result["stdout"]
    assert len(result["stdout"]) <= output_limit


def test_truncated_output_with_whitespace_expanded_base64_is_redacted_wholesale(
    tmp_path: Path,
) -> None:
    secret = "nexus-whitespace-expanded-base64-secret-" * 2
    encoded = harness.base64.b64encode(secret.encode("utf-8")).decode("ascii")
    expanded = "     ".join(encoded)
    output = ("p" * 500) + expanded + ("x" * 20)

    result = harness.run_process(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({output!r}); sys.stderr.write('safe stderr')",
        ],
        cwd=tmp_path,
        secrets=[secret],
        output_limit_chars=64,
        include_raw_output=True,
    )

    assert result["ok"] is True
    assert result["output_truncated"] is True
    assert result["stdout"] == "(redacted)"
    assert result["_raw_stdout"] == ""
    assert result["stderr"] == "safe stderr"


def test_output_redaction_preparation_finishes_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = False

    def interrupt_preparation(secret: str):
        raise KeyboardInterrupt

    def forbidden_popen(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("process started before redaction preparation completed")

    monkeypatch.setattr(harness, "_encoded_secret_variants", interrupt_preparation)
    monkeypatch.setattr(harness.subprocess, "Popen", forbidden_popen)

    with pytest.raises(KeyboardInterrupt):
        harness.run_process(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            secrets=["nexus-prelaunch-preparation-secret"],
        )

    assert launched is False


def test_required_workspace_git_failures_redact_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "nexus-required-git-secret"
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)

    def fail_git(*args, **kwargs):
        assert tuple(kwargs["secrets"]) == (secret,)
        assert kwargs["include_raw_output"] is True
        return {
            "ok": False,
            "returncode": 128,
            "timed_out": False,
            "stdout": "",
            "stderr": f"fatal: invalid alternate /tmp/{secret}",
            "duration_ms": 0.0,
            "launch_error": None,
            "output_truncated": False,
        }

    monkeypatch.setattr(harness, "git", fail_git)

    with pytest.raises(RuntimeError) as raised:
        harness.workspace_snapshot(
            workspace,
            "baseline",
            tmp_path / "artifacts",
            secrets=[secret],
        )

    assert secret not in str(raised.value)
    assert "/tmp/(redacted)" in str(raised.value)
