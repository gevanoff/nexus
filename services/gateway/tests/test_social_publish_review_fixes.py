from __future__ import annotations

from pathlib import Path

from app.config import S
from app.social_publish_config import SocialPublishSettings
from app.social_publish_route_fixes import (
    router,
    social_advance_publication,
    social_publish_config,
    tiktok_publication_status,
)


ROOT = Path(__file__).resolve().parents[3]


def _route_matches(path: str, method: str):
    return [
        route
        for route in router.routes
        if getattr(route, "path", "") == path
        and method in set(getattr(route, "methods", set()) or set())
    ]


def test_tiktok_inbox_handoff_is_not_published():
    assert tiktok_publication_status("PUBLISH_COMPLETE") == "PUBLISHED"
    assert tiktok_publication_status("SEND_TO_USER_INBOX") == "AWAITING_USER_ACTION"
    assert tiktok_publication_status("PROCESSING_UPLOAD") == "PROCESSING"
    assert tiktok_publication_status("PUBLISH_FAILED") == "FAILED_PERMANENT"


def test_reviewed_router_mounts_one_advance_handler():
    matches = _route_matches("/ui/api/social/publications/advance", "POST")
    assert len(matches) == 1
    assert matches[0].endpoint is social_advance_publication


def test_reviewed_router_mounts_one_capability_config_handler():
    matches = _route_matches("/ui/api/social/publishing/config", "GET")
    assert len(matches) == 1
    assert matches[0].endpoint is social_publish_config


def test_social_database_path_is_pinned_to_user_database(monkeypatch):
    monkeypatch.setenv("SOCIAL_PUBLISH_DB_PATH", "/tmp/separate-social.sqlite")
    settings = SocialPublishSettings.from_env()
    assert settings.db_path == str(S.USER_DB_PATH).strip()
    assert settings.db_path != "/tmp/separate-social.sqlite"


def test_direct_publishing_defaults_off(monkeypatch):
    monkeypatch.delenv("SOCIAL_DIRECT_PUBLISHING_ENABLED", raising=False)
    monkeypatch.delenv("SOCIAL_PUBLISHING_ENABLED", raising=False)
    settings = SocialPublishSettings.from_env()
    assert settings.direct_publishing_enabled is False
    assert settings.enabled is False
    assert "SOCIAL_DIRECT_PUBLISHING_ENABLED=true" in settings.provider_missing("youtube")


def test_direct_flag_takes_precedence_over_legacy_alias(monkeypatch):
    monkeypatch.setenv("SOCIAL_DIRECT_PUBLISHING_ENABLED", "false")
    monkeypatch.setenv("SOCIAL_PUBLISHING_ENABLED", "true")
    assert SocialPublishSettings.from_env().direct_publishing_enabled is False

    monkeypatch.setenv("SOCIAL_DIRECT_PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("SOCIAL_PUBLISHING_ENABLED", "false")
    assert SocialPublishSettings.from_env().direct_publishing_enabled is True


def test_legacy_direct_flag_remains_compatible(monkeypatch):
    monkeypatch.delenv("SOCIAL_DIRECT_PUBLISHING_ENABLED", raising=False)
    monkeypatch.setenv("SOCIAL_PUBLISHING_ENABLED", "true")
    assert SocialPublishSettings.from_env().direct_publishing_enabled is True


def test_assisted_publishing_is_always_visible_and_direct_ui_starts_hidden():
    html = (ROOT / "services/gateway/app/static/social_publish.html").read_text(encoding="utf-8")
    assert "Assisted publishing" in html
    assert "Always available" in html
    assert 'id="directPublishing" class="hidden"' in html
    assert "Open YouTube Studio" in html
    assert "Open Meta Business Suite" in html
    assert "Open TikTok Upload" in html
    assert "SOCIAL_ASSISTED_PUBLISHING_ENABLED" not in html


def test_browser_skips_direct_api_loading_when_direct_mode_is_disabled():
    javascript = (ROOT / "services/gateway/app/static/social_publish.js").read_text(encoding="utf-8")
    assert "direct_publishing_enabled" in javascript
    assert "if (directPublishingEnabled()) await Promise.all([loadAccounts(), loadMedia(), loadPublications()]);" in javascript
    assert "Assisted publishing remains available" in javascript
