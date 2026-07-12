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
