from __future__ import annotations

from pathlib import Path


def test_resources_ui_hides_duplicate_core_services_section() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static_root / "resources.html").read_text(encoding="utf-8")
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert 'id="control_plane_section"' in html
    assert 'id="core_services_section" class="resource-subsection" hidden' in html
    assert 'id="copy_hosts"' in html
    assert "Copy Host Info" in html
    assert "/static/resources.js?v=11" in html
    assert "splitCoreServicesForResourceUi" in js
    assert "controlPlaneCoreServiceIds" in js
    assert "hideWhenEmpty: true" in js
    assert "No core services reported." not in js


def test_resources_ui_can_copy_host_information() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    js = (static_root / "resources.js").read_text(encoding="utf-8")

    assert "buildHostInfoText" in js
    assert "# Nexus Host Information" in js
    assert "Hostname:" in js
    assert "OS:" in js
    assert "Platform:" in js
    assert "Processor:" in js
    assert "Memory:" in js
    assert "navigator.clipboard.writeText" in js
