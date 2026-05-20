from __future__ import annotations

from pathlib import Path


def test_coding_ui_trash_button_deletes_not_archives():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "coding.js").read_text(encoding="utf-8")

    assert 'trashBtn.title = "Delete workspace"' in source
    assert 'trashBtn.setAttribute("aria-label", "Delete workspace")' in source
    assert 'deleteTask(task.id);' in source
    assert 'Archive workspace for forensics' not in source


def test_sentinel_ui_lists_archives_and_actions():
    html = Path(__file__).resolve().parent.parent.joinpath("app", "static", "sentinel.html").read_text(encoding="utf-8")
    js = Path(__file__).resolve().parent.parent.joinpath("app", "static", "sentinel.js").read_text(encoding="utf-8")

    assert 'id="archives"' in html
    assert 'Archived Workspaces' in html
    assert 'category="archives"' not in js
    assert 'Analyze now' in js
    assert 'Erase archive' in js
    assert 'Retention: delete after' in js