from __future__ import annotations

from app.config import S
from app.social_publish_config import SocialPublishSettings
from app.social_publish_route_fixes import router, social_advance_publication, tiktok_publication_status


def test_tiktok_inbox_handoff_is_not_published():
    assert tiktok_publication_status("PUBLISH_COMPLETE") == "PUBLISHED"
    assert tiktok_publication_status("SEND_TO_USER_INBOX") == "AWAITING_USER_ACTION"
    assert tiktok_publication_status("PROCESSING_UPLOAD") == "PROCESSING"
    assert tiktok_publication_status("PUBLISH_FAILED") == "FAILED_PERMANENT"


def test_reviewed_router_mounts_one_advance_handler():
    matches = [
        route
        for route in router.routes
        if getattr(route, "path", "") == "/ui/api/social/publications/advance"
        and "POST" in set(getattr(route, "methods", set()) or set())
    ]
    assert len(matches) == 1
    assert matches[0].endpoint is social_advance_publication


def test_social_database_path_is_pinned_to_user_database(monkeypatch):
    monkeypatch.setenv("SOCIAL_PUBLISH_DB_PATH", "/tmp/separate-social.sqlite")
    settings = SocialPublishSettings.from_env()
    assert settings.db_path == str(S.USER_DB_PATH).strip()
    assert settings.db_path != "/tmp/separate-social.sqlite"
