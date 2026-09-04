from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path


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
                "expected": expected or {},
                "limits": {"wall_time_sec": 30, "max_agent_steps": 8},
                "tags": ["review-regression"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_relative_albatross_path_is_resolved_before_workspace_cwd(
    tmp_path: Path, monkeypatch
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


def test_retained_run_removes_git_objects_and_scrubs_committed_secret(tmp_path: Path) -> None:
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
    assert result["outcome"]["completed"] is True
    assert result["workspace"]["git_metadata_retained"] is False
    assert not (workspace / ".git").exists()
    assert not list(workspace.rglob(".git"))
    assert result_path.exists()

    for path in run_root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert token not in text, f"secret remained in retained artifact: {path}"
