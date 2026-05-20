from __future__ import annotations

from pathlib import Path


def test_tasks_ui_edit_button_is_wired_to_task_settings_modal():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "tasks.js").read_text(encoding="utf-8")

    assert 'editTask: document.getElementById("editTask")' in source
    assert 'els.editTask?.addEventListener("click", () => editSelectedTask().catch' in source
    assert 'mode: "task"' in source
    assert 'Task settings updated.' in source