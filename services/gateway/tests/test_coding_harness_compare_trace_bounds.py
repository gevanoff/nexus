from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "coding_harness_compare.py"
    spec = importlib.util.spec_from_file_location("nexus_coding_harness_compare_trace_bounds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_module()


def _event(name: str, payload_size: int = 0) -> str:
    return json.dumps(
        {
            "turn": 1,
            "kind": "toolCall",
            "name": name,
            "args": {"payload": "x" * payload_size},
        }
    ) + "\n"


def test_trace_over_per_file_budget_is_not_parsed_or_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    trace = sessions / "oversized.events.jsonl"
    trace.write_text(_event("file_read", payload_size=256), encoding="utf-8")
    monkeypatch.setattr(harness, "MAX_TRACE_FILE_BYTES", 128)
    monkeypatch.setattr(harness, "MAX_TRACE_TOTAL_BYTES", 1024)

    result = harness.parse_trace(sessions, artifact_dir=tmp_path / "artifacts")

    assert result["tool_calls"] == []
    assert result["trace_files"] == []
    assert result["trace_input_bytes"] == 0
    assert len(result["trace_omissions"]) == 1
    assert "per-file limit" in result["trace_omissions"][0]["reason"]
    assert not list((tmp_path / "artifacts").rglob("*.events.jsonl"))


def test_trace_aggregate_budget_skips_later_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    first = sessions / "a.events.jsonl"
    second = sessions / "b.events.jsonl"
    first.write_text(_event("file_read", payload_size=80), encoding="utf-8")
    second.write_text(_event("grep", payload_size=80), encoding="utf-8")
    first_size = first.stat().st_size
    second_size = second.stat().st_size
    monkeypatch.setattr(harness, "MAX_TRACE_FILE_BYTES", max(first_size, second_size) + 1)
    monkeypatch.setattr(harness, "MAX_TRACE_TOTAL_BYTES", first_size + second_size - 1)

    result = harness.parse_trace(sessions, artifact_dir=tmp_path / "artifacts")

    assert result["tool_calls"] == ["file_read"]
    assert len(result["trace_files"]) == 1
    assert result["trace_input_bytes"] == first_size
    assert len(result["trace_omissions"]) == 1
    assert "aggregate limit" in result["trace_omissions"][0]["reason"]


def test_trace_enumeration_has_entry_and_file_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for index in range(4):
        (sessions / f"entry-{index}.txt").touch()
    monkeypatch.setattr(harness, "MAX_TRACE_ENTRIES", 3)

    with pytest.raises(RuntimeError, match="entry limit"):
        harness.parse_trace(sessions)

    for path in sessions.iterdir():
        path.unlink()
    for index in range(3):
        (sessions / f"trace-{index}.events.jsonl").touch()
    monkeypatch.setattr(harness, "MAX_TRACE_ENTRIES", 10)
    monkeypatch.setattr(harness, "MAX_TRACE_FILES", 2)

    with pytest.raises(RuntimeError, match="file limit"):
        harness.parse_trace(sessions)


def test_trace_enumeration_obeys_parse_deadline(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "trace.events.jsonl").write_text(_event("file_read"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="time budget"):
        harness.parse_trace(sessions, deadline=0.0)
