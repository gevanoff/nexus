from __future__ import annotations

from pathlib import Path


STATIC = Path(__file__).resolve().parent.parent.joinpath("app", "static")


def test_social_studio_is_listed_in_both_apps_menus():
    chat_html = STATIC.joinpath("chat.html").read_text(encoding="utf-8")
    focused_nav = STATIC.joinpath("focused_nav.js").read_text(encoding="utf-8")

    assert '<a class="menu-item" href="/ui/social">Social Studio</a>' in chat_html
    assert '["Social Studio", "/ui/social"]' in focused_nav

    for html_path in STATIC.glob("*.html"):
        source = html_path.read_text(encoding="utf-8")
        if "/static/focused_nav.js?v=" in source:
            assert "/static/focused_nav.js?v=8" in source


def test_social_studio_has_explicit_profile_and_brief_actions():
    html = STATIC.joinpath("social.html").read_text(encoding="utf-8")
    js = STATIC.joinpath("social.js").read_text(encoding="utf-8")

    for element_id in ("saveBrand", "brandStatus", "newBrief", "saveBrief", "briefStatus"):
        assert f'id="{element_id}"' in html

    assert "Save brand profile" in html
    assert "Save video brief" in html
    assert "Generate platform drafts" in html
    assert 'els.saveBrand.addEventListener("click"' in js
    assert 'els.newBrief.addEventListener("click", newBrief)' in js
    assert 'els.saveBrief.addEventListener("click"' in js
    assert "Unsaved changes." in js
    assert '/static/social.js?v=3' in html


def test_social_studio_has_contextual_lm_field_actions():
    html = STATIC.joinpath("social.html").read_text(encoding="utf-8")
    js = STATIC.joinpath("social.js").read_text(encoding="utf-8")

    assert "button.lm-fill-button" in html
    assert "Use ✦ beside supported fields" in html
    assert "installLmFillButtons" in js
    assert 'fetchJson("/ui/api/social/field/generate"' in js
    assert '{ section: "brand", field: "audience", id: "brandAudience"' in js
    assert '{ section: "brief", field: "key_points", id: "keyPoints"' in js
    assert 'field: "description", id: "brandDescription"' not in js
    assert 'field: "transcript_notes", id: "transcriptNotes"' not in js
    assert "generated — review, then save" in js
