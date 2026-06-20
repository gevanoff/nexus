from __future__ import annotations

from pathlib import Path


def test_coding_ui_trash_button_deletes_not_archives():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "coding.js").read_text(encoding="utf-8")

    assert 'trashBtn.title = "Delete workspace"' in source
    assert 'trashBtn.setAttribute("aria-label", "Delete workspace")' in source
    assert 'archiveBtn.title = "Archive workspace for forensics"' in source
    assert 'archiveTask(task.id);' in source
    assert 'deleteTask(task.id);' in source


def test_coding_ui_has_huge_model_tracking_warning_controls():
    root = Path(__file__).resolve().parent.parent.joinpath("app", "static")
    html = root.joinpath("coding.html").read_text(encoding="utf-8")
    js = root.joinpath("coding.js").read_text(encoding="utf-8")

    assert '<select id="workspaceModelInput"></select>' in html
    assert 'id="workspaceModelHint"' in html
    assert 'id="trackCurrentCoderModel"' in html
    assert "coding_model_policy" in js
    assert "only run during idle periods" in js
    assert 'els.workspaceModelInput.value = "coder";' in js


def test_coding_ui_shows_workspace_model_identity_badges():
    root = Path(__file__).resolve().parent.parent.joinpath("app", "static")
    html = root.joinpath("coding.html").read_text(encoding="utf-8")
    js = root.joinpath("coding.js").read_text(encoding="utf-8")

    assert 'id="selectedModelLine"' in html
    assert "workspaceModelIdentity" in js
    assert "modelBadge(task)" in js
    assert "Resolved upstream:" in js
    assert "policy && policy.backend" in js


def test_coding_ui_has_long_horizon_dashboard_controls():
    root = Path(__file__).resolve().parent.parent.joinpath("app", "static")
    html = root.joinpath("coding.html").read_text(encoding="utf-8")
    js = root.joinpath("coding.js").read_text(encoding="utf-8")

    for element_id in (
        "agentMaxCycles",
        "agentMaxRuntimeMinutes",
        "agentContextResetCycles",
        "taskSearch",
        "taskFilter",
        "projectPlan",
        "runHistory",
        "runSelectedAgent",
        "pauseSelectedAgent",
    ):
        assert f'id="{element_id}"' in html

    assert "renderProjectPlan" in js
    assert "renderRunHistory" in js
    assert "nexus.coding.v2" in js
    assert "window.history.replaceState" in js
    assert "(state.tasks || []).some(agentIsActive)" in js


def test_focused_navigation_does_not_reserve_desktop_header_height_on_mobile():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "static", "focused_nav.js").read_text(encoding="utf-8")

    assert "main > header > :first-child { flex: 0 1 auto; min-width: 0; }" in source
    assert "main > header > .row { width: 100%; max-width: 100%; justify-content: flex-start; }" in source


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


def test_sentinel_ui_has_dark_select_options_and_reliable_immediate_analysis_actions():
    root = Path(__file__).resolve().parent.parent.joinpath("app", "static")
    html = root.joinpath("sentinel.html").read_text(encoding="utf-8")
    js = root.joinpath("sentinel.js").read_text(encoding="utf-8")

    assert "select option {" in html
    assert "background: #111821;" in html
    assert 'sentinel.js?v=3' in html
    assert "/analyze`" in js
    assert "requestArchiveAnalysis" in js
    assert "pollArchiveAnalysis" in js
    assert 'modeSelect.value === "immediate_local"' in js
    assert "runControlAction" in js
