from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_native_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _fixture(tmp_path: Path) -> Path:
    fixture = {
        "schema_version": 1,
        "id": "native-small-fix",
        "description": "Native runner test.",
        "repository": {
            "files": {
                "app.py": "VALUE = 'broken'\n",
                "test_app.py": "import unittest\n\nclass TestApp(unittest.TestCase):\n    pass\n",
            }
        },
        "mission": "Change VALUE from broken to fixed.",
        "expected": {
            "files_changed": ["app.py"],
            "allowed_files_changed": ["app.py"],
            "file_contains": [{"path": "app.py", "needle": "fixed"}],
            "validation": [["python3", "-m", "unittest", "-q"]],
        },
        "limits": {"wall_time_sec": 60, "max_agent_steps": 8},
        "tags": ["native"],
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


def _completed_task() -> dict:
    return {
        "id": "code_abcdef123456",
        "kind": "harness_eval",
        "status": "ready",
        "terminal_result": {"final_commit": "b" * 40},
        "agent_runs": [
            {
                "run_id": "coderun_1",
                "status": "completed",
                "backend": "mlx",
                "upstream_model": "mlx-community/Qwen3-Coder",
                "cycle": 4,
                "commit": "b" * 40,
                "stop_reason_code": "run_completed",
            }
        ],
        "agent": {
            "run_id": "coderun_1",
            "status": "completed",
            "backend": "mlx",
            "upstream_model": "mlx-community/Qwen3-Coder",
            "cycle": 4,
            "summary": "fixed",
            "error": "",
            "events": [
                {
                    "type": "started",
                    "backend": "vllm",
                    "upstream_model": "qwen-old",
                    "route_reason": "coding",
                },
                {"type": "assistant"},
                {"type": "tool_finished", "name": "coding_read_file"},
                {"type": "tool_finished", "name": "coding_replace_text"},
                {
                    "type": "semantic_reroute",
                    "previous_backend": "vllm",
                    "previous_upstream_model": "qwen-old",
                    "backend": "mlx",
                    "upstream_model": "mlx-community/Qwen3-Coder",
                },
                {"type": "context_reset"},
                {"type": "assistant"},
                {"type": "tool_finished", "name": "coding_run_command"},
                {"type": "tool_finished", "name": "coding_git_diff"},
            ],
        },
    }


def test_validation_fixture_guards_non_string_numeric_compatibility():
    fixture = harness.load_fixture(
        Path(harness.__file__).with_name("coding_harness_fixtures") / "validation-after-edit.json"
    )

    assert "test_non_string_numeric_value_is_preserved" in fixture["repository"]["files"]["test_ports.py"]
    assert "normalize_port(4.5), 4" in fixture["repository"]["files"]["test_ports.py"]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "back\\slash.py",
        "control\nname.py",
        ".nexus/state.json",
        ".GIT/config",
        "nested//name.py",
    ],
)
def test_fixture_loader_rejects_paths_the_native_server_cannot_materialize(
    tmp_path: Path,
    unsafe_path: str,
):
    fixture_path = _fixture(tmp_path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["repository"]["files"] = {unsafe_path: "content\n"}
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="fixture path"):
        harness.load_fixture(fixture_path)


def test_run_nexus_fixture_normalizes_route_validation_and_cleanup(monkeypatch, tmp_path):
    calls: list[tuple[str, str, dict | None]] = []
    task = _completed_task()
    fixture_path = _fixture(tmp_path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["expected"]["files_changed"] = ["app.py", "new.py"]
    fixture["expected"]["allowed_files_changed"] = ["app.py", "new.py"]
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=30.0):
        calls.append((method, path, body))
        if method == "POST" and path == "/coding/harness/runs":
            return {"task": {**task, "agent": {"status": "queued"}}}
        if method == "GET" and path.startswith("/coding/harness/tasks/") and path.endswith("/diff"):
            return {
                "ok": True,
                "merge_base": "a" * 40,
                "compare_ref": "main",
                "changes": {"files": [{"path": "app.py", "kind": "modified"}]},
                "diff": {
                    "stdout": "diff --git a/app.py b/app.py\n-VALUE = 'broken'\n+VALUE = 'fixed'\n"
                    },
                }
        if method == "GET" and path.startswith("/coding/harness/tasks/") and path.endswith("/changes"):
            return {
                "result": {
                    "ok": True,
                    "files": [
                        {"path": "app.py", "status": " M", "kind": "modified"},
                        {"path": "new.py", "status": "??", "kind": "untracked"},
                    ],
                }
            }
        if method == "GET" and "/file?path=app.py" in path:
            return {
                "path": "app.py",
                "size": 16,
                "mode": "100644",
                "encoding": "utf-8",
                "content": "VALUE = 'fixed'\n",
            }
        if method == "GET" and "/file?path=new.py" in path:
            return {
                "path": "new.py",
                "size": 15,
                "mode": "100755",
                "encoding": "utf-8",
                "content": "CREATED = True\n",
            }
        if method == "GET" and path.startswith("/coding/tasks/"):
            return {"task": task}
        if method == "POST" and path.endswith("/validation"):
            return {
                "result": {
                    "ok": True,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "OK\n",
                    "duration_ms": 4.5,
                }
            }
        if method == "DELETE" and path.startswith("/coding/harness/tasks/"):
            return {"result": {"ok": True, "task_id": task["id"]}}
        raise AssertionError((method, path, body))

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness, "MAX_SNAPSHOT_CHANGED_FILES", 2)

    result, result_path = harness.run_nexus_fixture(
        fixture_path,
        out_root=tmp_path / "results",
        nexus_base_url="http://gateway/v1",
        nexus_token="secret-token-value",
        model="coder",
    )

    assert result_path.exists()
    assert result["harness"] == "nexus-coding-workspace"
    assert result["outcome"] == {
        "status": "completed",
        "completed": True,
        "interrupted": False,
        "exit_code": 0,
        "error": None,
        "stop_reason_code": "run_completed",
    }
    assert result["model"]["backend"] == "mlx"
    assert result["model"]["upstream_model"] == "mlx-community/Qwen3-Coder"
    assert result["model"]["route_evidence"] == "coding_workspace_persisted_run_record"
    assert [item["event"] for item in result["model"]["route_history"]] == [
        "started",
        "semantic_reroute",
    ]
    assert result["trajectory"]["agent_steps"] == 4
    assert result["trajectory"]["tool_calls"] == 4
    assert result["trajectory"]["context_resets"] == 1
    assert result["objective"]["passed"] is True
    assert result["validation"]["passed"] is True
    assert result["workspace"]["files_changed"] == ["app.py", "new.py"]
    assert result["workspace"]["git_metadata_retained"] is False
    assert result["workspace"]["execution_workspace_retained"] is False
    diff_text = Path(result["artifacts"]["diff"]).read_text(encoding="utf-8")
    assert "+VALUE = 'fixed'\n" in diff_text
    assert diff_text.count("diff --git a/new.py b/new.py") == 1
    assert "new file mode 100755" in diff_text
    assert "+CREATED = True\n" in diff_text
    assert (
        Path(result["artifacts"]["run_root"])
        / "artifacts"
        / "final-files"
        / "new.py"
    ).read_text(encoding="utf-8") == "CREATED = True\n"
    assert any(method == "DELETE" for method, _, _ in calls)


def test_nexus_file_reader_enforces_aggregate_budget_before_caching(monkeypatch):
    calls = []

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=300.0):
        calls.append(path)
        rel = "a.txt" if "a.txt" in path else "b.txt"
        return {
            "path": rel,
            "size": 3,
            "mode": "100644",
            "encoding": "utf-8",
            "content": "abc",
        }

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness, "MAX_SNAPSHOT_FILE_BYTES", 5)
    cache = {}
    non_text_paths = set()
    read_content = harness._nexus_final_file_reader(
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
        cache=cache,
        non_text_paths=non_text_paths,
        file_modes={},
        deadline=harness.time.monotonic() + 10,
    )

    assert read_content("a.txt") == ("abc", None)
    with pytest.raises(RuntimeError, match="file-evidence limit"):
        read_content("b.txt")

    assert cache == {"a.txt": ("abc", None)}
    assert non_text_paths == set()
    assert all("/coding/harness/tasks/" in path for path in calls)


def test_nexus_file_reader_stops_when_snapshot_deadline_is_exhausted(monkeypatch):
    called = False

    def fake_request(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("request must not start after the snapshot deadline")

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    read_content = harness._nexus_final_file_reader(
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
        cache={},
        non_text_paths=set(),
        file_modes={},
        deadline=harness.time.monotonic() - 1,
    )

    with pytest.raises(RuntimeError, match="snapshot time budget exhausted"):
        read_content("a.txt")
    assert called is False


def test_nexus_evidence_time_is_bounded_and_excluded_from_validation(monkeypatch, tmp_path):
    task = _completed_task()
    clock = {"now": 100.0}
    evidence_timeouts = []
    validation_deadline = []

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=300.0):
        if method == "POST" and path == "/coding/harness/runs":
            return {"task": {**task, "agent": {"status": "queued"}}}
        if method == "GET" and path.endswith("/diff"):
            evidence_timeouts.append(timeout_sec)
            clock["now"] += 2
            return {
                "ok": True,
                "merge_base": "a" * 40,
                "compare_ref": "main",
                "changes": {"files": [{"path": "app.py", "kind": "modified"}]},
                "diff": {
                    "stdout": "diff --git a/app.py b/app.py\n-VALUE = 'broken'\n+VALUE = 'fixed'\n",
                    "stdout_truncated": False,
                },
            }
        if method == "GET" and path.endswith("/changes"):
            evidence_timeouts.append(timeout_sec)
            clock["now"] += 2
            return {"result": {"ok": True, "files": []}}
        if method == "GET" and "/file?path=app.py" in path:
            evidence_timeouts.append(timeout_sec)
            clock["now"] += 2
            return {
                "path": "app.py",
                "size": 16,
                "mode": "100644",
                "encoding": "utf-8",
                "content": "VALUE = 'fixed'\n",
            }
        if method == "DELETE" and path.startswith("/coding/harness/tasks/"):
            return {"result": {"ok": True, "task_id": task["id"]}}
        raise AssertionError((method, path, body))

    def fake_validation(fixture, task_id, *, base_url, token, deadline):
        validation_deadline.append(deadline)
        return {"commands": [], "passed": True, "budget_exhausted": False, "timed_out": False}

    monkeypatch.setattr(harness.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness, "_wait_for_nexus_task", lambda *args, **kwargs: (task, False))
    monkeypatch.setattr(harness, "run_nexus_validation", fake_validation)

    result, _ = harness.run_nexus_fixture(
        _fixture(tmp_path),
        out_root=tmp_path / "results",
        nexus_base_url="http://gateway/v1",
        nexus_token="secret-token-value",
    )

    assert result["outcome"]["completed"] is True
    assert evidence_timeouts == [30.0, 28.0, 26.0]
    assert validation_deadline == [166.0]


@pytest.mark.parametrize("encoding", ["binary", "symlink"])
def test_run_nexus_fixture_omits_non_text_untracked_content(monkeypatch, tmp_path, encoding):
    task = _completed_task()
    fixture_path = _fixture(tmp_path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["expected"] = {
        "files_changed": ["blob.bin"],
        "allowed_files_changed": ["blob.bin"],
        "validation": [],
    }
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=300.0):
        if method == "POST" and path == "/coding/harness/runs":
            return {"task": {**task, "agent": {"status": "queued"}}}
        if method == "GET" and path.endswith("/diff"):
            return {
                "ok": True,
                "merge_base": "a" * 40,
                "compare_ref": "main",
                "changes": {"files": []},
                "diff": {"stdout": "", "stdout_truncated": False},
            }
        if method == "GET" and path.endswith("/changes"):
            return {
                "result": {
                    "ok": True,
                    "files": [{"path": "blob.bin", "status": "??", "kind": "untracked"}],
                }
            }
        if method == "GET" and "/file?path=blob.bin" in path:
            return {
                "path": "blob.bin",
                "size": 4,
                "encoding": encoding,
                "content": None,
            }
        if method == "GET" and path.startswith("/coding/tasks/"):
            return {"task": task}
        if method == "DELETE" and path.startswith("/coding/harness/tasks/"):
            return {"result": {"ok": True, "task_id": task["id"]}}
        raise AssertionError((method, path, body))

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)

    result, _ = harness.run_nexus_fixture(
        fixture_path,
        out_root=tmp_path / "results",
        nexus_base_url="http://gateway/v1",
        nexus_token="secret-token-value",
    )

    assert result["outcome"]["completed"] is True
    assert result["workspace"]["evidence_omissions"] == [
        {
            "path": "blob.bin",
            "reason": f"{encoding} file content omitted; untracked patch omitted",
        }
    ]
    assert result["workspace"]["final_file_omissions"] == [
        {"path": "blob.bin", "reason": f"{encoding} file content omitted"}
    ]
    assert Path(result["artifacts"]["diff"]).read_text(encoding="utf-8") == ""
    assert not (
        Path(result["artifacts"]["run_root"])
        / "artifacts"
        / "final-files"
        / "blob.bin"
    ).exists()


def test_omitted_artifact_paths_redact_single_byte_cross_file_fragments():
    token = "secret-token-value"

    redacted = harness._redact_omitted_artifact_paths(list(token), [token])

    assert token not in "".join(redacted)
    assert set(redacted) == {"(redacted)"}


def test_run_nexus_fixture_deletes_failed_workspace(monkeypatch, tmp_path):
    deleted = []

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=30.0):
        if method == "POST":
            return {
                "task": {
                    "id": "code_abcdef123456",
                    "kind": "harness_eval",
                    "status": "error",
                    "error": "git init failed",
                }
            }
        if method == "DELETE":
            deleted.append(path)
            return {"result": {"ok": True}}
        raise AssertionError((method, path))

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)

    with pytest.raises(harness.NexusApiError, match="git init failed"):
        harness.run_nexus_fixture(
            _fixture(tmp_path),
            out_root=tmp_path / "results",
            nexus_base_url="http://gateway/v1",
            nexus_token="secret-token-value",
        )

    assert deleted == ["/coding/harness/tasks/code_abcdef123456"]
    assert list((tmp_path / "results").iterdir()) == []


def test_run_nexus_fixture_rejects_limits_it_cannot_apply_exactly(monkeypatch, tmp_path):
    fixture_path = _fixture(tmp_path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["limits"] = {"wall_time_sec": 59, "max_agent_steps": 3}
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setattr(
        harness,
        "nexus_api_request",
        lambda *args, **kwargs: pytest.fail("fixture should fail before API use"),
    )

    with pytest.raises(ValueError, match="max_agent_steps >= 4"):
        harness.run_nexus_fixture(
            fixture_path,
            out_root=tmp_path / "results",
            nexus_base_url="http://gateway/v1",
            nexus_token="secret-token-value",
        )

    assert not (tmp_path / "results").exists()


def test_run_nexus_fixture_rejects_truncated_diff_and_cleans_up(monkeypatch, tmp_path):
    deleted = []
    task = _completed_task()

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=30.0):
        if method == "POST" and path == "/coding/harness/runs":
            return {"task": {**task, "agent": {"status": "queued"}}}
        if method == "GET" and path.endswith("/diff"):
            return {
                "ok": True,
                "changes": {"files": [{"path": "app.py", "kind": "modified"}]},
                "diff": {"stdout": "partial", "stdout_truncated": True},
            }
        if method == "GET" and path.endswith("/changes"):
            return {"result": {"ok": True, "files": []}}
        if method == "GET" and path.startswith("/coding/tasks/"):
            return {"task": task}
        if method == "DELETE":
            deleted.append(path)
            return {"result": {"ok": True}}
        raise AssertionError((method, path))

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)

    with pytest.raises(harness.NexusApiError, match="diff evidence was truncated"):
        harness.run_nexus_fixture(
            _fixture(tmp_path),
            out_root=tmp_path / "results",
            nexus_base_url="http://gateway/v1",
            nexus_token="secret-token-value",
        )

    assert deleted == ["/coding/harness/tasks/code_abcdef123456"]
    assert list((tmp_path / "results").iterdir()) == []


def test_run_nexus_validation_uses_harness_budget_endpoint(monkeypatch):
    captured = {}

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=30.0):
        captured.update({"method": method, "path": path, "body": body, "timeout": timeout_sec})
        return {
            "result": {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "duration_ms": 1,
            }
        }

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    fixture = {
        "expected": {"validation": [["python3", "-m", "unittest", "-q"]]},
    }

    result = harness.run_nexus_validation(
        fixture,
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
        deadline=harness.time.monotonic() + 180,
    )

    assert result["passed"] is True
    assert captured["path"] == "/coding/harness/tasks/code_abcdef123456/validation"
    assert captured["body"]["timeout_sec"] > 120


def test_delete_nexus_harness_task_retries_active_runner(monkeypatch):
    attempts = 0

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=30.0):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise harness.NexusApiError("active", status=409)
        return {"result": {"ok": True}}

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)

    harness._delete_nexus_harness_task(
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
    )

    assert attempts == 3


def test_delete_nexus_harness_task_retries_beyond_previous_fixed_attempt_cap(
    monkeypatch,
):
    attempts = 0

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=300.0):
        nonlocal attempts
        attempts += 1
        if attempts <= 100:
            raise harness.NexusApiError("active", status=409)
        return {"result": {"ok": True}}

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)

    harness._delete_nexus_harness_task(
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
    )

    assert attempts == 101


def test_nexus_diff_waits_for_guarded_worker_conflicts(monkeypatch):
    attempts = 0

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=300.0):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise harness.NexusApiError("active worker", status=409)
        return {"ok": True, "diff": {"stdout": ""}, "changes": {"files": []}}

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)

    payload, request_started = harness._nexus_harness_diff_after_workers(
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
        wait_timeout_sec=120,
    )

    assert payload["ok"] is True
    assert request_started <= harness.time.monotonic()
    assert attempts == 3


def test_wait_for_nexus_task_preserves_interrupted_terminal_status(monkeypatch):
    task = {"id": "code_abcdef123456", "agent": {"status": "interrupted"}}
    monkeypatch.setattr(
        harness,
        "nexus_api_request",
        lambda *args, **kwargs: {"task": task},
    )

    observed, timed_out = harness._wait_for_nexus_task(
        task["id"],
        base_url="http://gateway/v1",
        token="token",
        deadline=harness.time.monotonic() + 1.0,
    )

    assert observed is task
    assert timed_out is False


@pytest.mark.parametrize("status", ["failed_finalization", "failed_publish"])
def test_wait_for_nexus_task_recognizes_controller_terminal_failures(monkeypatch, status):
    task = {"id": "code_abcdef123456", "agent": {"status": status}}
    monkeypatch.setattr(
        harness,
        "nexus_api_request",
        lambda *args, **kwargs: {"task": task},
    )

    observed, timed_out = harness._wait_for_nexus_task(
        task["id"],
        base_url="http://gateway/v1",
        token="token",
        deadline=harness.time.monotonic() + 1.0,
    )

    assert observed is task
    assert timed_out is False


def test_wait_for_nexus_task_settles_after_timeout_pause(monkeypatch):
    statuses = iter(["pausing", "paused"])

    def fake_request(method, base_url, path, *, token, body=None, timeout_sec=30.0):
        return {
            "task": {
                "id": "code_abcdef123456",
                "agent": {"status": next(statuses)},
            }
        }

    monkeypatch.setattr(harness, "nexus_api_request", fake_request)
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)

    observed, timed_out = harness._wait_for_nexus_task(
        "code_abcdef123456",
        base_url="http://gateway/v1",
        token="token",
        deadline=harness.time.monotonic() - 1.0,
    )

    assert observed["agent"]["status"] == "paused"
    assert timed_out is True


def test_nexus_api_url_preserves_v1_prefix():
    assert harness._nexus_api_url("http://gateway:8800/v1/", "/coding/harness/runs") == (
        "http://gateway:8800/v1/coding/harness/runs"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/socket",
        "http://user:password@gateway/v1",
        "http://gateway/v1?redirect=http://attacker",
        "http://gateway/v1#fragment",
    ],
)
def test_nexus_api_url_rejects_unsafe_authority_forms(base_url):
    with pytest.raises(ValueError, match="without credentials"):
        harness._nexus_api_url(base_url, "/coding/harness/runs")


def test_run_paired_writes_manifest_for_both_normalized_results(monkeypatch, tmp_path):
    fixture_path = _fixture(tmp_path)
    out_root = tmp_path / "paired-results"
    nexus_result = {
        "harness": "nexus-coding-workspace",
        "outcome": {"status": "completed", "completed": True},
        "objective": {"passed": True},
        "duration_ms": 10,
        "trajectory": {"agent_steps": 2, "tool_calls": 3},
        "workspace": {"files_changed": ["app.py"]},
        "validation": {"passed": True},
    }
    albatross_result = {
        "harness": "albatross",
        "outcome": {"status": "completed", "completed": True},
        "objective": {"passed": True},
        "duration_ms": 12,
        "trajectory": {"agent_steps": 2, "tool_calls": 4},
        "workspace": {"files_changed": ["app.py"]},
        "validation": {"passed": True},
    }

    monkeypatch.setattr(
        harness,
        "parse_args",
        lambda: SimpleNamespace(
            command="run-paired",
            fixture=str(fixture_path),
            albatross_bin="albatross",
            base_url="http://gateway/v1",
            token="secret-token-value",
            model="coder",
            out_root=str(out_root),
            json=False,
        ),
    )

    def fake_nexus(*args, out_root, **kwargs):
        path = out_root / "nexus-result.json"
        harness.write_json(path, nexus_result)
        return nexus_result, path

    def fake_albatross(*args, out_root, **kwargs):
        path = out_root / "albatross-result.json"
        harness.write_json(path, albatross_result)
        return albatross_result, path

    monkeypatch.setattr(harness, "run_nexus_fixture", fake_nexus)
    monkeypatch.setattr(harness, "run_albatross_fixture", fake_albatross)

    assert harness.main() == 0

    manifests = list(out_root.glob("pair-*/comparison.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["fixture_id"] == "native-small-fix"
    assert manifest["completed"] is True
    assert {Path(path).name for path in manifest["results"]} == {
        "nexus-result.json",
        "albatross-result.json",
    }
