from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
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


def _fixture(path: Path, *, files: dict[str, str], expected: dict | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "review-fix",
                "description": "review regression fixture",
                "repository": {"files": files},
                "mission": "Inspect the repository and complete the requested task.",
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


def test_application_events_jsonl_is_not_counted_as_albatross_trace(tmp_path: Path) -> None:
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


def test_validation_commands_share_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = {
        "expected": {"validation": [["first"], ["second"]]},
    }
    observed_timeouts: list[float] = []
    monotonic_values = iter([9.0, 9.4])

    monkeypatch.setattr(harness.time, "monotonic", lambda: next(monotonic_values))

    def fake_run_process(argv, *, cwd, env=None, timeout_sec=60.0, secrets=()):
        observed_timeouts.append(timeout_sec)
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0.0,
        }

    monkeypatch.setattr(harness, "run_process", fake_run_process)
    result = harness.run_validation(
        fixture,
        tmp_path,
        tmp_path / "home",
        tmp_path / "tmp",
        deadline=10.0,
    )

    assert result["passed"] is True
    assert observed_timeouts == pytest.approx([1.0, 0.6])


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


def test_retained_run_removes_git_objects_scrubs_secret_and_hashes_final_diff(tmp_path: Path) -> None:
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

root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
secret = os.environ['OPENAI_API_KEY']
(root / 'leak.txt').write_text(secret + '\\n', encoding='utf-8')
subprocess.run(['git', 'add', 'leak.txt'], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
subprocess.run(['git', 'commit', '-m', 'capture secret'], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
sessions = root / '.sessions'
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
    workspace = run_root / "workspace"
    diff_path = Path(result["artifacts"]["diff"])
    assert result["outcome"]["completed"] is True
    assert result["workspace"]["git_metadata_retained"] is False
    assert not (workspace / ".git").exists()
    assert not list(workspace.rglob(".git"))
    assert result_path.exists()
    assert result["workspace"]["diff_sha256"] == hashlib.sha256(diff_path.read_bytes()).hexdigest()

    for path in run_root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert token not in text, f"secret remained in retained artifact: {path}"
