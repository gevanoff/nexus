from __future__ import annotations

import httpx
import pytest

from app.social_publish_config import SocialPublishSettings
from app import social_publish_providers as providers


def _settings():
    return SocialPublishSettings(
        enabled=True,
        db_path="/tmp/test.sqlite",
        media_dir="/tmp/media",
        media_max_bytes=100,
        media_ttl_sec=100,
        public_base_url="https://example.test",
        token_encryption_key="unused",
        oauth_state_ttl_sec=60,
        http_timeout_sec=30,
        google_client_id="google-id",
        google_client_secret="google-secret",
        google_redirect_uri="https://example.test/ui/social/oauth/youtube/callback",
        google_scopes=["youtube.upload", "youtube.readonly"],
        meta_app_id="meta-id",
        meta_app_secret="meta-secret",
        meta_redirect_uri="https://example.test/ui/social/oauth/meta/callback",
        meta_api_version="v99.0",
        meta_scopes=["pages_show_list"],
        tiktok_client_key="tt-key",
        tiktok_client_secret="tt-secret",
        tiktok_redirect_uri="https://example.test/ui/social/oauth/tiktok/callback",
        tiktok_scopes=["user.info.basic", "video.publish"],
    )


def test_authorization_urls_include_state_and_provider_scopes():
    settings = _settings()
    google = providers.build_google_authorize_url(settings, "state-value")
    assert "state=state-value" in google
    assert "youtube.upload" in google
    meta = providers.build_meta_authorize_url(settings, "state-value")
    assert "/v99.0/dialog/oauth" in meta
    assert "pages_show_list" in meta
    tiktok = providers.build_tiktok_authorize_url(settings, "state-value")
    assert "video.publish" in tiktok
    assert "state=state-value" in tiktok


def test_tiktok_chunk_plan_obeys_whole_and_multi_chunk_rules():
    assert providers.tiktok_chunk_plan(4 * providers.MIB) == {
        "chunk_size": 4 * providers.MIB,
        "total_chunk_count": 1,
    }
    assert providers.tiktok_chunk_plan(64 * providers.MIB)["total_chunk_count"] == 1
    plan = providers.tiktok_chunk_plan(100 * providers.MIB)
    assert 5 * providers.MIB <= plan["chunk_size"] <= 64 * providers.MIB
    assert plan["total_chunk_count"] >= 2
    assert plan["chunk_size"] * plan["total_chunk_count"] <= 100 * providers.MIB


def test_tiktok_validation_requires_current_creator_options():
    creator = {
        "data": {
            "privacy_level_options": ["SELF_ONLY"],
            "comment_disabled": True,
            "duet_disabled": False,
            "stitch_disabled": False,
            "max_video_post_duration_sec": 60,
        }
    }
    with pytest.raises(providers.ProviderError):
        providers.validate_tiktok_post(
            {"privacy_level": "PUBLIC_TO_EVERYONE", "title": "Example"},
            creator,
            {"metadata": {"duration_sec": 30}},
        )
    with pytest.raises(providers.ProviderError):
        providers.validate_tiktok_post(
            {"privacy_level": "SELF_ONLY", "allow_comment": True, "title": "Example"},
            creator,
            {"metadata": {"duration_sec": 30}},
        )
    with pytest.raises(providers.ProviderError):
        providers.validate_tiktok_post(
            {"privacy_level": "SELF_ONLY", "title": "Example"},
            creator,
            {"metadata": {"duration_sec": 61}},
        )


@pytest.mark.asyncio
async def test_youtube_resumable_start_requires_location_header(tmp_path):
    media = {"filename": "clip.mp4", "size_bytes": 5, "mime_type": "video/mp4"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["uploadType"] == "resumable"
        return httpx.Response(200, headers={"Location": "https://upload.example/session"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        location = await providers.youtube_begin_upload(
            client,
            access_token="token",
            media=media,
            metadata={"title": "Title", "privacy_status": "private"},
        )
    assert location == "https://upload.example/session"
