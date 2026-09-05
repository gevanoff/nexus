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
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_compare", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _fixture(tmp_path: Path, *, expected: dict | None = None) -> Path:
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test-fixture",
                "description": "test",
                "repository": {"files": {"app.py": "VALUE = 'broken'\n"}},
                "mission": "Change VALUE from broken to fixed.",
                "expected": expected
                or {
                    "files_changed": ["app.py"],
                    "file_contains": [{"path": "app.py", "needle": "fixed"}],
                },
                "limits": {"wall_time_sec": 30, "max_agent_steps": 8},
                "tags": ["test"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _fake_albatross(tmp_path: Path) -> Path:
    path = tmp_path / "albatross"
    path.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --eval --json --allow-tools')
    raise SystemExit(0)
root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
config = json.loads((root / 'agent.config.json').read_text())
events = pathlib.Path(config['sessionDir']) / 'fake.events.jsonl'
events.parent.mkdir(parents=True, exist_ok=True)
if (root / 'probe.txt').exists():
    events.write_text(json.dumps({'turn': 1, 'kind': 'toolCall', 'callId': '1', 'name': 'file_read', 'args': {'path': 'probe.txt'}, 'depth': 0}) + '\\n' + json.dumps({'turn': 1, 'kind': 'turnSummary', 'steps': 1, 'modelMs': 1, 'toolMs': 1, 'approvalMs': 0, 'totalMs': 2, 'hitStepLimit': False}) + '\\n')
    print((root / 'probe.txt').read_text().strip())
    raise SystemExit(0)
app = root / 'app.py'
if app.exists():
    app.write_text(app.read_text().replace('broken', 'fixed'))
events.write_text(json.dumps({'turn': 1, 'kind': 'toolCall', 'callId': '1', 'name': 'file_edit', 'args': {'path': 'app.py'}, 'depth': 0}) + '\\n' + json.dumps({'turn': 1, 'kind': 'turnSummary', 'steps': 2, 'modelMs': 1, 'toolMs': 1, 'approvalMs': 0, 'totalMs': 2, 'hitStepLimit': False}) + '\\n')
print('done')
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_albatross_with_session_transcript(tmp_path: Path) -> Path:
    path = tmp_path / "albatross-session"
    path.write_text(
        """#!/usr/bin/env python3
import datetime, json, os, pathlib, sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('albatross --print --eval --json --allow-tools')
    raise SystemExit(0)
root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
config = json.loads((root / 'agent.config.json').read_text())
sessions = pathlib.Path(config['sessionDir'])
sessions.mkdir(parents=True, exist_ok=True)
call_id = 'call-1'
messages = [
    {'role': 'system', 'content': 'system'},
    {'role': 'user', 'content': 'read the probe'},
    {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function',
        'function': {'name': 'file_read', 'arguments': '{"path":"probe.txt"}'},
    }]},
    {'role': 'tool', 'tool_call_id': call_id, 'content': (root / 'probe.txt').read_text()},
    {'role': 'assistant', 'content': 'done'},
]
with (sessions / 'fake.jsonl').open('w') as handle:
    for message in messages:
        handle.write(json.dumps({'timestamp': datetime.datetime.now().isoformat(), 'message': message}) + '\\n')
print((root / 'probe.txt').read_text().strip())
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_fixture_rejects_agent_config_override(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    data = json.loads(path.read_text())
    data["repository"]["files"]["agent.config.json"] = "{}"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="harness state/config"):
        harness.load_fixture(path)


def test_build_env_drops_unrelated_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    env = harness.build_albatross_env(
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="nexus-secret",
        model="coder",
        workspace=tmp_path / "work",
        home=tmp_path / "home",
        temp_dir=tmp_path / "tmp",
        max_steps=10,
    )
    assert env["BACKEND"] == "openai"
    assert env["OPENAI_BASE_URL"] == "http://ai2:8800/v1"
    assert env["OPENAI_API_KEY"] == "nexus-secret"
    assert env["AGENT_MODEL"] == "coder"
    assert env["OUTSIDE_WORKSPACE"] == "deny"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_redaction_removes_exact_and_bearer_tokens() -> None:
    token = "nexus-super-secret-value"
    text = f"OPENAI_API_KEY={token} Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    redacted = harness.redact_text(text, [token])
    assert token not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "(redacted)" in redacted


def test_run_process_timeout_is_typed(tmp_path: Path) -> None:
    result = harness.run_process(
        [sys.executable, "-c", "import time; time.sleep(2)"], cwd=tmp_path, timeout_sec=0.1
    )
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["returncode"] is None


def test_initialized_workspace_is_clean_and_ignores_albatross(tmp_path: Path) -> None:
    fixture = harness.load_fixture(_fixture(tmp_path))
    work = tmp_path / "work"
    harness.initialize_workspace(work, fixture)
    (work / ".albatross").mkdir()
    (work / ".albatross" / "scratch.txt").write_text("runtime")
    status = harness.git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=work)
    assert status["stdout"].strip() == ""


def test_runtime_config_pins_private_trace_dir_and_is_not_evidence(tmp_path: Path) -> None:
    fixture = harness.load_fixture(_fixture(tmp_path))
    work = tmp_path / "work"
    harness.initialize_workspace(work, fixture)
    trace_root = tmp_path / "private-home" / ".config" / "albatross" / "sessions"

    harness._write_albatross_runtime_config(work, trace_root)

    config_path = work / "agent.config.json"
    assert json.loads(config_path.read_text()) == {
        "sessionDir": str(trace_root.resolve())
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    status = harness.git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=work
    )
    assert status["stdout"].strip() == ""

    nested_config = work / "nested" / "agent.config.json"
    nested_config.parent.mkdir()
    nested_config.write_text("{}\n", encoding="utf-8")
    status = harness.git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=work
    )
    assert status["stdout"].splitlines() == ["?? nested/agent.config.json"]


def test_parse_trace_extracts_tools_steps_and_compaction(tmp_path: Path) -> None:
    trace = tmp_path / "one.events.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"turn": 1, "kind": "toolCall", "name": "file_read"}),
                json.dumps({"turn": 1, "kind": "toolCall", "name": "shell"}),
                json.dumps({"turn": 1, "kind": "contextCompacted"}),
                json.dumps({"turn": 1, "kind": "turnSummary", "steps": 3}),
                "not-json",
            ]
        )
        + "\n"
    )
    result = harness.parse_trace(tmp_path)
    assert result["tool_calls"] == ["file_read", "shell"]
    assert result["agent_steps"] == 3
    assert result["context_resets"] == 1
    assert result["malformed_trace_lines"] == 1


def test_session_transcript_fallback_requires_matching_tool_result(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    transcript = sessions / "one.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "now",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "matched",
                                    "type": "function",
                                    "function": {
                                        "name": "file_read",
                                        "arguments": "{}",
                                    },
                                },
                                {
                                    "id": "unmatched",
                                    "type": "function",
                                    "function": {
                                        "name": "file_write",
                                        "arguments": "{}",
                                    },
                                },
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "now",
                        "message": {
                            "role": "tool",
                            "tool_call_id": "matched",
                            "content": "contents",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "now",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = harness.parse_session_transcripts(
        sessions, artifact_dir=tmp_path / "artifacts"
    )

    assert result["tool_calls"] == ["file_read"]
    assert result["agent_turns"] == 1
    assert result["agent_steps"] == 2
    assert result["malformed_trace_lines"] == 1
    assert len(result["trace_files"]) == 1
    retained = Path(result["trace_files"][0]).read_text(encoding="utf-8")
    assert "file_read" in retained
    assert "contents" not in retained


def test_objective_checks_reject_extra_changed_file(tmp_path: Path) -> None:
    fixture = harness.load_fixture(_fixture(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text("VALUE = 'fixed'\n")
    result = harness.objective_checks(fixture, work, ["app.py", "extra.py"], {"passed": None})
    assert result["passed"] is False


def test_missing_albatross_probe_is_non_destructive(tmp_path: Path) -> None:
    report = harness.probe(str(tmp_path / "missing-albatross"))
    assert report["ok"] is False
    assert report["albatross"]["installed"] is False
    assert report["albatross"]["raw"] == "albatross unavailable"


def test_comparison_rendering() -> None:
    result = {
        "harness": "albatross",
        "outcome": {"status": "completed"},
        "objective": {"passed": True},
        "duration_ms": 12,
        "trajectory": {"agent_steps": 2, "tool_calls": 3},
        "workspace": {"files_changed": ["x.py"]},
        "validation": {"passed": True},
    }
    rendered = harness.render_comparison([result])
    assert "albatross" in rendered
    assert "completed" in rendered
    assert "validation" in rendered


@pytest.mark.requires_linux_process_containment
def test_fake_executable_full_run_is_isolated_and_redacted(tmp_path: Path) -> None:
    fake = _fake_albatross(tmp_path)
    token = "nexus-test-secret-123456"
    result, result_path = harness.run_albatross_fixture(
        _fixture(tmp_path),
        out_root=tmp_path / "results",
        executable=str(fake),
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
    )
    assert result["outcome"]["completed"] is True
    assert result["objective"]["passed"] is True
    assert result["workspace"]["files_changed"] == ["app.py"]
    assert "file_edit" in result["trajectory"]["tool_call_names"]
    assert result["model"]["gateway"] == "nexus"
    assert token not in result_path.read_text(encoding="utf-8")
    assert token not in Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8")


@pytest.mark.requires_linux_process_containment
def test_fake_live_probe_requires_read_tool_and_leaves_streaming_unknown(tmp_path: Path) -> None:
    fake = _fake_albatross(tmp_path)
    token = "nexus-live-secret-123456"
    report = harness.probe(
        str(fake),
        live=True,
        out_root=tmp_path / "probe-results",
        nexus_base_url="http://ai2:8800/v1",
        nexus_token=token,
        model="coder",
    )
    assert report["ok"] is True
    assert report["capabilities"]["chat"] is True
    assert report["capabilities"]["streaming"] is None
    assert report["capabilities"]["tool_calls"] is True
    assert report["capabilities"]["structured_trace"] is True
    assert token not in json.dumps(report)


@pytest.mark.requires_linux_process_containment
def test_fake_live_probe_accepts_private_session_transcript_fallback(
    tmp_path: Path,
) -> None:
    fake = _fake_albatross_with_session_transcript(tmp_path)
    report = harness.probe(
        str(fake),
        live=True,
        out_root=tmp_path / "probe-results",
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="nexus-live-secret-123456",
        model="coder",
    )
    assert report["ok"] is True
    assert report["capabilities"]["tool_calls"] is True
    assert report["capabilities"]["structured_trace"] is True
