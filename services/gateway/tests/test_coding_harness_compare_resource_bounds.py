from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_compare_resource_bounds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _write_fixture(path: Path, *, files: dict[str, str], expected: dict | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "resource-bound-fixture",
                "description": "resource bound regression fixture",
                "repository": {"files": files},
                "mission": "Inspect the repository and satisfy the objective.",
                "expected": {"files_changed": []} if expected is None else expected,
                "limits": {"wall_time_sec": 30, "max_agent_steps": 8},
                "tags": ["resource-bounds"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_fixture_json_size_is_bounded_before_parsing(tmp_path: Path) -> None:
    fixture = tmp_path / "oversized.json"
    fixture.write_bytes(b"x" * (harness.MAX_FIXTURE_JSON_BYTES + 1))

    with pytest.raises(ValueError, match="fixture JSON exceeds"):
        harness.load_fixture(fixture)


def test_fixture_inline_file_size_is_bounded(tmp_path: Path) -> None:
    fixture = _write_fixture(
        tmp_path / "fixture.json",
        files={"huge.txt": "x" * (harness.MAX_FIXTURE_FILE_BYTES + 1)},
    )

    with pytest.raises(ValueError, match="fixture file huge.txt exceeds"):
        harness.load_fixture(fixture)


def test_fixture_inline_total_size_is_bounded(tmp_path: Path) -> None:
    chunk = "x" * 1_700_000
    fixture = _write_fixture(
        tmp_path / "fixture.json",
        files={f"file-{index}.txt": chunk for index in range(5)},
    )

    with pytest.raises(ValueError, match="fixture inline files exceed"):
        harness.load_fixture(fixture)


def test_objective_file_content_checks_fail_closed_above_read_limit(tmp_path: Path) -> None:
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * (harness.MAX_OBJECTIVE_FILE_BYTES + 1))
    fixture = {
        "expected": {
            "file_contains": [{"path": "large.txt", "needle": "needle"}],
        }
    }

    result = harness.objective_checks(
        fixture,
        tmp_path,
        changed=[],
        validation={"passed": None},
    )

    assert result["passed"] is False
    assert result["checks"][0]["passed"] is False
    assert "objective-read limit" in result["checks"][0]["error"]


def test_live_probe_requires_no_workspace_mutation(tmp_path: Path) -> None:
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
    print('albatross --print --allow-tools --eval --json')
    raise SystemExit(0)
root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
(root / 'probe.txt').write_text('NEXUS_ALBATROSS_PROBE_OK\\nMUTATED\\n', encoding='utf-8')
sessions = root / '.sessions'
sessions.mkdir(parents=True, exist_ok=True)
(sessions / 'probe.events.jsonl').write_text(
    json.dumps({'turn': 1, 'kind': 'toolCall', 'callId': '1', 'name': 'file_read', 'args': {'path': 'probe.txt'}, 'depth': 0}) + '\\n',
    encoding='utf-8',
)
print('NEXUS_ALBATROSS_PROBE_OK')
""",
    )

    report = harness.probe(
        str(fake),
        live=True,
        out_root=tmp_path / "runs",
        nexus_base_url="http://ai2:8800/v1",
        nexus_token="probe-token",
        model="coder",
    )

    assert report["capabilities"]["tool_calls"] is True
    assert report["ok"] is False
    result = json.loads(Path(report["live_result"]).read_text(encoding="utf-8"))
    assert result["objective"]["passed"] is False
    files_check = next(item for item in result["objective"]["checks"] if item["kind"] == "files_changed")
    assert files_check["expected"] == []
    assert files_check["actual"] == ["probe.txt"]


def test_binary_secret_artifact_is_omitted_from_retained_evidence(tmp_path: Path) -> None:
    fake = _write_executable(
        tmp_path / "albatross",
        """#!/usr/bin/env python3
import os
import pathlib
import sys
if '--version' in sys.argv:
    print('albatross 2.4.0')
    raise SystemExit(0)
root = pathlib.Path(os.environ['WORKSPACE_ROOT'])
secret = os.environ['OPENAI_API_KEY'].encode('utf-8')
(root / 'leak.bin').write_bytes(b'\\xff\\xfe\\x00' + secret + b'\\x00\\xff')
print('done')
""",
    )
    fixture = _write_fixture(
        tmp_path / "fixture.json",
        files={"app.py": "VALUE = 1\n"},
        expected={"allowed_files_changed": ["leak.bin"]},
    )
    token = "nexus-binary-artifact-secret"

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
    artifacts = run_root / "artifacts"
    assert result["outcome"]["completed"] is True
    assert "final-files/leak.bin" in result["artifacts"]["omitted_non_text"]
    assert not (artifacts / "final-files" / "leak.bin").exists()
    assert not (run_root / "workspace").exists()
    assert not (run_root / "home").exists()
    assert not (run_root / "tmp").exists()
    for path in artifacts.rglob("*"):
        if not path.is_file():
            continue
        assert token.encode("utf-8") not in path.read_bytes(), f"secret remained in retained artifact: {path}"
