from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List

from app.config import S


TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv(name: str, default: str) -> List[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class SocialPublishSettings:
    enabled: bool
    db_path: str
    media_dir: str
    media_max_bytes: int
    media_ttl_sec: int
    public_base_url: str
    token_encryption_key: str
    oauth_state_ttl_sec: int
    http_timeout_sec: int

    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_scopes: List[str]

    meta_app_id: str
    meta_app_secret: str
    meta_redirect_uri: str
    meta_api_version: str
    meta_scopes: List[str]

    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_redirect_uri: str
    tiktok_scopes: List[str]

    @classmethod
    def from_env(cls) -> "SocialPublishSettings":
        # Social publishing tables reference users(id), so they must live in the
        # same SQLite database initialized by user_store. A separate path would
        # have no users table and would fail on the first OAuth/account/media
        # insert. SOCIAL_PUBLISH_DB_PATH is therefore intentionally unsupported.
        db_path = str(S.USER_DB_PATH).strip()
        return cls(
            enabled=_env_bool("SOCIAL_PUBLISHING_ENABLED", False),
            db_path=db_path,
            media_dir=(os.getenv("SOCIAL_MEDIA_DIR") or "/var/lib/gateway/data/social_media").strip(),
            media_max_bytes=_env_int("SOCIAL_MEDIA_MAX_BYTES", 4_000_000_000),
            media_ttl_sec=_env_int("SOCIAL_MEDIA_TTL_SEC", 60 * 60 * 24 * 7),
            public_base_url=(os.getenv("SOCIAL_PUBLIC_BASE_URL") or S.PUBLIC_BASE_URL or "").strip().rstrip("/"),
            token_encryption_key=(os.getenv("SOCIAL_TOKEN_ENCRYPTION_KEY") or "").strip(),
            oauth_state_ttl_sec=_env_int("SOCIAL_OAUTH_STATE_TTL_SEC", 15 * 60),
            http_timeout_sec=_env_int("SOCIAL_PROVIDER_HTTP_TIMEOUT_SEC", 120),
            google_client_id=(os.getenv("SOCIAL_GOOGLE_CLIENT_ID") or "").strip(),
            google_client_secret=(os.getenv("SOCIAL_GOOGLE_CLIENT_SECRET") or "").strip(),
            google_redirect_uri=(os.getenv("SOCIAL_GOOGLE_REDIRECT_URI") or "").strip(),
            google_scopes=_csv(
                "SOCIAL_GOOGLE_SCOPES",
                "openid,email,profile,https://www.googleapis.com/auth/youtube.readonly,https://www.googleapis.com/auth/youtube.upload",
            ),
            meta_app_id=(os.getenv("SOCIAL_META_APP_ID") or "").strip(),
            meta_app_secret=(os.getenv("SOCIAL_META_APP_SECRET") or "").strip(),
            meta_redirect_uri=(os.getenv("SOCIAL_META_REDIRECT_URI") or "").strip(),
            meta_api_version=(os.getenv("SOCIAL_META_API_VERSION") or "").strip(),
            meta_scopes=_csv(
                "SOCIAL_META_SCOPES",
                "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish",
            ),
            tiktok_client_key=(os.getenv("SOCIAL_TIKTOK_CLIENT_KEY") or "").strip(),
            tiktok_client_secret=(os.getenv("SOCIAL_TIKTOK_CLIENT_SECRET") or "").strip(),
            tiktok_redirect_uri=(os.getenv("SOCIAL_TIKTOK_REDIRECT_URI") or "").strip(),
            tiktok_scopes=_csv("SOCIAL_TIKTOK_SCOPES", "user.info.basic,video.publish"),
        )

    def provider_missing(self, provider: str) -> List[str]:
        provider = (provider or "").strip().lower()
        common: List[str] = []
        if not self.enabled:
            common.append("SOCIAL_PUBLISHING_ENABLED=true")
        if not self.token_encryption_key:
            common.append("SOCIAL_TOKEN_ENCRYPTION_KEY")
        if provider == "youtube":
            required = {
                "SOCIAL_GOOGLE_CLIENT_ID": self.google_client_id,
                "SOCIAL_GOOGLE_CLIENT_SECRET": self.google_client_secret,
                "SOCIAL_GOOGLE_REDIRECT_URI": self.google_redirect_uri,
            }
        elif provider == "meta":
            required = {
                "SOCIAL_META_APP_ID": self.meta_app_id,
                "SOCIAL_META_APP_SECRET": self.meta_app_secret,
                "SOCIAL_META_REDIRECT_URI": self.meta_redirect_uri,
                "SOCIAL_META_API_VERSION": self.meta_api_version,
            }
        elif provider == "tiktok":
            required = {
                "SOCIAL_TIKTOK_CLIENT_KEY": self.tiktok_client_key,
                "SOCIAL_TIKTOK_CLIENT_SECRET": self.tiktok_client_secret,
                "SOCIAL_TIKTOK_REDIRECT_URI": self.tiktok_redirect_uri,
            }
        else:
            return [f"unknown provider: {provider}"]
        return common + [name for name, value in required.items() if not value]

    def readiness(self) -> Dict[str, Dict[str, object]]:
        result: Dict[str, Dict[str, object]] = {}
        for provider in ("youtube", "meta", "tiktok"):
            missing = self.provider_missing(provider)
            result[provider] = {"ready": not missing, "missing": missing}
        result["signed_media"] = {
            "ready": bool(self.public_base_url.startswith("https://") and self.token_encryption_key),
            "missing": [
                name
                for name, ok in (
                    ("SOCIAL_PUBLIC_BASE_URL (HTTPS)", self.public_base_url.startswith("https://")),
                    ("SOCIAL_TOKEN_ENCRYPTION_KEY", bool(self.token_encryption_key)),
                )
                if not ok
            ],
        }
        return result
