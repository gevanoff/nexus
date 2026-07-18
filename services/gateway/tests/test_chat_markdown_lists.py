from __future__ import annotations

from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_chat_markdown_groups_loose_ordered_lists() -> None:
    script = (STATIC / "chat.js").read_text(encoding="utf-8")
    html = (STATIC / "chat.html").read_text(encoding="utf-8")

    assert "function collectMarkdownList(lines, startIndex, ordered)" in script
    assert "while (i < lines.length && !lines[i].trim()) i += 1;" in script
    assert "items.push(ordered ? match[2] : match[1]);" in script
    assert "parsed.items.forEach((text) =>" in script
    assert "list.start = parsed.startNumber" in script
    assert ".markdown ol.md-list { list-style: decimal; }" in html
    assert '/static/chat.js?v=19' in html
