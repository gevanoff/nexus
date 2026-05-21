from __future__ import annotations

from pathlib import Path


def test_coding_ui_trash_button_deletes_not_archives():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "coding.js").read_text(encoding="utf-8")

    assert 'trashBtn.title = "Delete workspace"' in source
    assert 'trashBtn.setAttribute("aria-label", "Delete workspace")' in source
    assert 'archiveBtn.title = "Archive workspace for forensics"' in source
    assert 'archiveTask(task.id);' in source
    assert 'deleteTask(task.id);' in source


def test_sentinel_ui_lists_archives_and_actions():
    html = Path(__file__).resolve().parent.parent.joinpath("app", "static", "sentinel.html").read_text(encoding="utf-8")
    js = Path(__file__).resolve().parent.parent.joinpath("app", "static", "sentinel.js").read_text(encoding="utf-8")

    assert 'id="archives"' in html
    assert 'Archived Workspaces' in html
    assert 'category="archives"' not in js
    assert 'Analyze now' in js
    assert 'Erase archive' in js
    assert 'Human follow-up' in js
    assert 'Flag for external agent' in js
    assert 'Mark superseded' in js
    assert 'Mark invalid' in js
    assert 'Analysis target' not in js
    assert 'Retention: delete after' in js