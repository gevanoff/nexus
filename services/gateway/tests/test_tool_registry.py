import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app.config import S
from app.tool_calling import registry


@pytest.mark.asyncio
async def test_file_read_is_bounded_and_blocks_traversal(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(root))

    result = await registry.builtin_tool_definitions()["nexus_file_read"].implementation(
        {"path": "README.md", "start_line": 2, "end_line": 3, "max_chars": 100}
    )
    assert result["ok"] is True
    assert result["content"] == "two\nthree"

    with pytest.raises(ValueError, match="outside allowlisted"):
        await registry.builtin_tool_definitions()["nexus_file_read"].implementation(
            {"path": str(tmp_path / "outside.txt"), "start_line": None, "end_line": None, "max_chars": 100}
        )


def test_secret_redaction_covers_structured_and_text_values():
    redacted = registry.redact_secrets({"token": "abc", "line": "Authorization: Bearer-secret"})
    assert redacted["token"] == "[REDACTED]"
    assert "Bearer-secret" not in redacted["line"]


@pytest.mark.asyncio
async def test_file_grep_returns_structured_invalid_regex(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(tmp_path))

    result = await registry.builtin_tool_definitions()["nexus_file_grep"].implementation(
        {"root": ".", "pattern": "[", "glob": None, "limit": 10}
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_regex"


@pytest.mark.asyncio
async def test_file_grep_matches_glob_relative_to_selected_root(monkeypatch, tmp_path):
    wanted = tmp_path / "src"
    unwanted = tmp_path / "other" / "src"
    wanted.mkdir()
    unwanted.mkdir(parents=True)
    (wanted / "wanted.py").write_text("needle\n", encoding="utf-8")
    (unwanted / "unwanted.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(S, "NEXUS_TOOL_FS_ROOTS", str(tmp_path))

    result = await registry.builtin_tool_definitions()["nexus_file_grep"].implementation(
        {"root": ".", "pattern": "needle", "glob": "src/*.py", "limit": 10}
    )

    assert result["ok"] is True
    assert [match["path"] for match in result["matches"]] == ["src/wanted.py"]
