from __future__ import annotations

from pathlib import Path


def test_tasks_ui_edit_button_is_wired_to_task_settings_modal():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "tasks.js").read_text(encoding="utf-8")

    assert 'editTask: document.getElementById("editTask")' in source
    assert 'els.editTask?.addEventListener("click", () => editSelectedTask().catch' in source
    assert 'mode: "task"' in source
    assert 'taskEditPrompt: document.getElementById("taskEditPrompt")' in source
    assert '/ui/api/agent-tasks/${encodeURIComponent(state.selectedId)}/prompt' in source
    assert 'setStatus("task prompt is required", true)' in source
    assert 'taskProtected(task)' in source
    assert 'Task settings updated.' in source


def test_tasks_ui_coder_type_applies_coding_tool_defaults():
    root = Path(__file__).resolve().parent.parent.joinpath("app", "static")
    js = root.joinpath("tasks.js").read_text(encoding="utf-8")
    html = root.joinpath("tasks.html").read_text(encoding="utf-8")

    assert "applyTaskTypeDefaults" in js
    assert 'els.taskType?.addEventListener("change", applyTaskTypeDefaults)' in js
    assert 'els.codingMode?.addEventListener("change", applyTaskTypeDefaults)' in js
    assert "requiredToolsForTaskType" in js
    assert "requiredTierForTaskType" in js
    assert "modelIntegrationPayload" in js
    assert "coding_task_create" in html
    assert "modelIntegrationFields" in html
    assert 'tasks.js?v=9' in html
    assert 'select option,' in html


def test_tasks_detail_metadata_is_available_for_badges():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "tasks.js").read_text(encoding="utf-8")
    render_detail = source[source.index("  function renderDetail") : source.index("  function taskTypeConfig")]

    assert "const meta = task.metadata || {};" in render_detail
    assert render_detail.index("const meta = task.metadata || {};") < render_detail.index("if (els.detailBadges)")
