from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from app.social_publish_config import SocialPublishSettings


MIB = 1024 * 1024
TIKTOK_MIN_CHUNK = 5 * MIB
TIKTOK_MAX_CHUNK = 64 * MIB


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int = 502,
        retryable: bool = False,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = int(status_code)
        self.retryable = bool(retryable)
        self.detail = detail

    def payload(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "message": str(self),
            "status_code": self.status_code,
            "retryable": self.retryable,
            "detail": self.detail,
        }


def _json(response: httpx.Response, provider: str) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text[:4000]}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    if response.status_code >= 400:
        retryable = response.status_code == 429 or response.status_code >= 500
        nested_error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        message = (
            payload.get("error_description")
            or nested_error.get("message")
            or payload.get("message")
            or f"{provider} returned HTTP {response.status_code}"
        )
        raise ProviderError(
            str(message),
            provider=provider,
            status_code=response.status_code,
            retryable=retryable,
            detail=payload,
        )
    return payload


def _tiktok_json(response: httpx.Response) -> Dict[str, Any]:
    payload = _json(response, "tiktok")
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("code") or "ok") != "ok":
        code = str(error.get("code") or "unknown")
        retryable = code in {"rate_limit_exceeded", "internal_error"}
        raise ProviderError(
            str(error.get("message") or code),
            provider="tiktok",
            status_code=response.status_code,
            retryable=retryable,
            detail=payload,
        )
    return payload


def _expires_at(expires_in: Any) -> Optional[int]:
    try:
        seconds = int(expires_in)
    except Exception:
        return None
    return int(time.time()) + max(0, seconds)


def build_google_authorize_url(settings: SocialPublishSettings, state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(settings.google_scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


async def exchange_google_code(client: httpx.AsyncClient, settings: SocialPublishSettings, code: str) -> Dict[str, Any]:
    response = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.google_redirect_uri,
        },
    )
    payload = _json(response, "youtube")
    payload["access_expires_ts"] = _expires_at(payload.get("expires_in"))
    return payload


async def refresh_google_token(client: httpx.AsyncClient, settings: SocialPublishSettings, refresh_token: str) -> Dict[str, Any]:
    response = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    payload = _json(response, "youtube")
    payload["access_expires_ts"] = _expires_at(payload.get("expires_in"))
    return payload


async def discover_youtube_accounts(client: httpx.AsyncClient, access_token: str) -> List[Dict[str, Any]]:
    response = await client.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id,snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    payload = _json(response, "youtube")
    accounts: List[Dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        accounts.append(
            {
                "provider": "youtube",
                "external_account_id": str(item["id"]),
                "display_name": str(snippet.get("title") or item["id"]),
                "account_type": "channel",
                "metadata": {"channel": item},
            }
        )
    return accounts


def build_meta_authorize_url(settings: SocialPublishSettings, state: str) -> str:
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "response_type": "code",
        "scope": ",".join(settings.meta_scopes),
        "state": state,
    }
    return f"https://www.facebook.com/{settings.meta_api_version}/dialog/oauth?" + urlencode(params)


async def exchange_meta_code(client: httpx.AsyncClient, settings: SocialPublishSettings, code: str) -> Dict[str, Any]:
    base = f"https://graph.facebook.com/{settings.meta_api_version}"
    response = await client.get(
        f"{base}/oauth/access_token",
        params={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "redirect_uri": settings.meta_redirect_uri,
            "code": code,
        },
    )
    short = _json(response, "meta")
    short_token = str(short.get("access_token") or "")
    response = await client.get(
        f"{base}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": short_token,
        },
    )
    payload = _json(response, "meta")
    payload["access_expires_ts"] = _expires_at(payload.get("expires_in"))
    return payload


async def discover_meta_accounts(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    user_access_token: str,
) -> List[Dict[str, Any]]:
    base = f"https://graph.facebook.com/{settings.meta_api_version}"
    response = await client.get(
        f"{base}/me/accounts",
        params={
            "fields": "id,name,access_token,tasks,instagram_business_account{id,username,name}",
            "access_token": user_access_token,
            "limit": 100,
        },
    )
    payload = _json(response, "meta")
    accounts: List[Dict[str, Any]] = []
    for page in payload.get("data") or []:
        if not isinstance(page, dict) or not page.get("id") or not page.get("access_token"):
            continue
        page_id = str(page["id"])
        page_name = str(page.get("name") or page_id)
        page_token = str(page["access_token"])
        accounts.append(
            {
                "provider": "facebook",
                "external_account_id": page_id,
                "display_name": page_name,
                "account_type": "page",
                "access_token": page_token,
                "metadata": {"page_id": page_id, "tasks": page.get("tasks") or []},
            }
        )
        instagram = page.get("instagram_business_account")
        if isinstance(instagram, dict) and instagram.get("id"):
            ig_id = str(instagram["id"])
            ig_name = str(instagram.get("username") or instagram.get("name") or ig_id)
            accounts.append(
                {
                    "provider": "instagram",
                    "external_account_id": ig_id,
                    "display_name": ig_name,
                    "account_type": "professional_account",
                    "access_token": page_token,
                    "metadata": {"page_id": page_id, "page_name": page_name, "instagram": instagram},
                }
            )
    return accounts


def build_tiktok_authorize_url(settings: SocialPublishSettings, state: str) -> str:
    params = {
        "client_key": settings.tiktok_client_key,
        "response_type": "code",
        "scope": ",".join(settings.tiktok_scopes),
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": state,
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)


async def exchange_tiktok_code(client: httpx.AsyncClient, settings: SocialPublishSettings, code: str) -> Dict[str, Any]:
    response = await client.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.tiktok_redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    payload = _json(response, "tiktok")
    payload["access_expires_ts"] = _expires_at(payload.get("expires_in"))
    payload["refresh_expires_ts"] = _expires_at(payload.get("refresh_expires_in"))
    return payload


async def refresh_tiktok_token(client: httpx.AsyncClient, settings: SocialPublishSettings, refresh_token: str) -> Dict[str, Any]:
    response = await client.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    payload = _json(response, "tiktok")
    payload["access_expires_ts"] = _expires_at(payload.get("expires_in"))
    payload["refresh_expires_ts"] = _expires_at(payload.get("refresh_expires_in"))
    return payload


async def discover_tiktok_account(client: httpx.AsyncClient, access_token: str, open_id: str) -> List[Dict[str, Any]]:
    response = await client.get(
        "https://open.tiktokapis.com/v2/user/info/",
        params={"fields": "open_id,union_id,avatar_url,display_name,username"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    payload = _tiktok_json(response)
    user = (payload.get("data") or {}).get("user") if isinstance(payload.get("data"), dict) else None
    user = user if isinstance(user, dict) else {}
    external_id = str(user.get("open_id") or open_id)
    return [
        {
            "provider": "tiktok",
            "external_account_id": external_id,
            "display_name": str(user.get("display_name") or user.get("username") or external_id),
            "account_type": "creator",
            "metadata": {"user": user},
        }
    ]


async def revoke_provider_token(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    provider: str,
    access_token: str,
) -> None:
    if provider == "youtube":
        response = await client.post("https://oauth2.googleapis.com/revoke", params={"token": access_token})
        if response.status_code >= 400:
            _json(response, "youtube")
        return
    if provider == "tiktok":
        response = await client.post(
            "https://open.tiktokapis.com/v2/oauth/revoke/",
            data={
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                "token": access_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            _json(response, "tiktok")
        return
    if provider in {"facebook", "instagram"}:
        base = f"https://graph.facebook.com/{settings.meta_api_version}"
        response = await client.delete(f"{base}/me/permissions", params={"access_token": access_token})
        if response.status_code >= 400:
            _json(response, "meta")


async def youtube_begin_upload(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    media: Dict[str, Any],
    metadata: Dict[str, Any],
) -> str:
    snippet = {
        "title": str(metadata.get("title") or media.get("filename") or "Untitled video")[:100],
        "description": str(metadata.get("description") or "")[:5000],
        "tags": [str(item)[:500] for item in metadata.get("tags") or []][:500],
        "categoryId": str(metadata.get("category_id") or "22"),
    }
    status: Dict[str, Any] = {
        "privacyStatus": str(metadata.get("privacy_status") or "private"),
        "selfDeclaredMadeForKids": bool(metadata.get("made_for_kids", False)),
    }
    if metadata.get("publish_at"):
        status["publishAt"] = str(metadata["publish_at"])
    response = await client.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(media["size_bytes"]),
            "X-Upload-Content-Type": str(media["mime_type"]),
        },
        json={"snippet": snippet, "status": status},
    )
    if response.status_code >= 400:
        _json(response, "youtube")
    location = response.headers.get("Location")
    if not location:
        raise ProviderError("YouTube did not return a resumable upload URL", provider="youtube", detail=response.text[:1000])
    return location


async def youtube_upload_file(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    upload_url: str,
    media: Dict[str, Any],
) -> Dict[str, Any]:
    with open(media["path"], "rb") as handle:
        response = await client.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": str(media["mime_type"]),
                "Content-Length": str(media["size_bytes"]),
            },
            content=handle,
        )
    return _json(response, "youtube")


async def youtube_status(client: httpx.AsyncClient, access_token: str, video_id: str) -> Dict[str, Any]:
    response = await client.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "processingDetails,status", "id": video_id},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return _json(response, "youtube")


async def facebook_begin_reel(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    page_id: str,
    page_token: str,
) -> Dict[str, Any]:
    response = await client.post(
        f"https://graph.facebook.com/{settings.meta_api_version}/{page_id}/video_reels",
        params={"access_token": page_token, "upload_phase": "start"},
    )
    return _json(response, "facebook")


async def facebook_upload_reel(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    page_token: str,
    video_id: str,
    upload_url: Optional[str],
    media: Dict[str, Any],
) -> Dict[str, Any]:
    target = upload_url or f"https://rupload.facebook.com/video-upload/{settings.meta_api_version}/{video_id}"
    with open(media["path"], "rb") as handle:
        response = await client.post(
            target,
            headers={
                "Authorization": f"OAuth {page_token}",
                "offset": "0",
                "file_size": str(media["size_bytes"]),
                "Content-Type": "application/octet-stream",
            },
            content=handle,
        )
    return _json(response, "facebook")


async def facebook_finish_reel(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    page_id: str,
    page_token: str,
    video_id: str,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    params = {
        "access_token": page_token,
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": str(metadata.get("video_state") or "PUBLISHED"),
        "description": str(metadata.get("description") or ""),
        "title": str(metadata.get("title") or ""),
    }
    if metadata.get("scheduled_publish_time"):
        params["scheduled_publish_time"] = str(metadata["scheduled_publish_time"])
    response = await client.post(
        f"https://graph.facebook.com/{settings.meta_api_version}/{page_id}/video_reels",
        params=params,
    )
    return _json(response, "facebook")


async def facebook_status(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    page_token: str,
    video_id: str,
) -> Dict[str, Any]:
    response = await client.get(
        f"https://graph.facebook.com/{settings.meta_api_version}/{video_id}",
        params={"access_token": page_token, "fields": "id,status,permalink_url"},
    )
    return _json(response, "facebook")


async def instagram_create_reel_container(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    ig_user_id: str,
    page_token: str,
    video_url: str,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "access_token": page_token,
        "media_type": "REELS",
        "video_url": video_url,
        "caption": str(metadata.get("caption") or "")[:2200],
        "share_to_feed": "true" if bool(metadata.get("share_to_feed", True)) else "false",
    }
    if metadata.get("thumb_offset") is not None:
        params["thumb_offset"] = str(int(metadata["thumb_offset"]))
    response = await client.post(
        f"https://graph.facebook.com/{settings.meta_api_version}/{ig_user_id}/media",
        params=params,
    )
    return _json(response, "instagram")


async def instagram_container_status(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    page_token: str,
    creation_id: str,
) -> Dict[str, Any]:
    response = await client.get(
        f"https://graph.facebook.com/{settings.meta_api_version}/{creation_id}",
        params={"access_token": page_token, "fields": "status_code,status"},
    )
    return _json(response, "instagram")


async def instagram_publish_container(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    ig_user_id: str,
    page_token: str,
    creation_id: str,
) -> Dict[str, Any]:
    response = await client.post(
        f"https://graph.facebook.com/{settings.meta_api_version}/{ig_user_id}/media_publish",
        params={"access_token": page_token, "creation_id": creation_id},
    )
    return _json(response, "instagram")


async def tiktok_creator_info(client: httpx.AsyncClient, access_token: str) -> Dict[str, Any]:
    response = await client.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
    )
    return _tiktok_json(response)


def utf16_units(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


def tiktok_chunk_plan(size_bytes: int) -> Dict[str, int]:
    size = int(size_bytes)
    if size <= 0:
        raise ValueError("video size must be positive")
    if size <= TIKTOK_MAX_CHUNK:
        return {"chunk_size": size, "total_chunk_count": 1}
    count = int(math.ceil(size / TIKTOK_MAX_CHUNK))
    chunk_size = size // count
    if chunk_size < TIKTOK_MIN_CHUNK:
        raise ValueError("video would require chunks smaller than TikTok's minimum")
    while size // chunk_size > count:
        chunk_size += 1
    return {"chunk_size": chunk_size, "total_chunk_count": size // chunk_size}


def validate_tiktok_post(metadata: Dict[str, Any], creator_info: Dict[str, Any], media: Dict[str, Any]) -> None:
    data = creator_info.get("data") if isinstance(creator_info.get("data"), dict) else {}
    privacy = str(metadata.get("privacy_level") or "")
    options = [str(item) for item in data.get("privacy_level_options") or []]
    if not privacy or privacy not in options:
        raise ProviderError(
            "Select a TikTok privacy level returned for this creator",
            provider="tiktok",
            status_code=400,
            detail={"privacy_level_options": options},
        )
    title = str(metadata.get("title") or "")
    if utf16_units(title) > 2200:
        raise ProviderError("TikTok caption exceeds 2200 UTF-16 units", provider="tiktok", status_code=400)
    if bool(metadata.get("allow_comment")) and bool(data.get("comment_disabled")):
        raise ProviderError("Comments are disabled for this TikTok creator", provider="tiktok", status_code=400)
    if bool(metadata.get("allow_duet")) and bool(data.get("duet_disabled")):
        raise ProviderError("Duet is disabled for this TikTok creator", provider="tiktok", status_code=400)
    if bool(metadata.get("allow_stitch")) and bool(data.get("stitch_disabled")):
        raise ProviderError("Stitch is disabled for this TikTok creator", provider="tiktok", status_code=400)
    max_duration = data.get("max_video_post_duration_sec")
    duration = (media.get("metadata") or {}).get("duration_sec")
    if max_duration is not None and duration is not None and float(duration) > float(max_duration):
        raise ProviderError(
            "Video duration exceeds this TikTok creator's current limit",
            provider="tiktok",
            status_code=400,
            detail={"duration_sec": duration, "max_video_post_duration_sec": max_duration},
        )


async def tiktok_begin_post(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    media: Dict[str, Any],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    plan = tiktok_chunk_plan(media["size_bytes"])
    post_info: Dict[str, Any] = {
        "title": str(metadata.get("title") or ""),
        "privacy_level": str(metadata.get("privacy_level") or ""),
        "disable_duet": not bool(metadata.get("allow_duet", False)),
        "disable_comment": not bool(metadata.get("allow_comment", False)),
        "disable_stitch": not bool(metadata.get("allow_stitch", False)),
        "brand_content_toggle": bool(metadata.get("brand_content_toggle", False)),
        "brand_organic_toggle": bool(metadata.get("brand_organic_toggle", False)),
        "is_aigc": bool(metadata.get("is_aigc", False)),
    }
    if metadata.get("video_cover_timestamp_ms") is not None:
        post_info["video_cover_timestamp_ms"] = int(metadata["video_cover_timestamp_ms"])
    response = await client.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        json={
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": int(media["size_bytes"]),
                **plan,
            },
        },
    )
    payload = _tiktok_json(response)
    payload["chunk_plan"] = plan
    return payload


async def tiktok_upload_file(
    client: httpx.AsyncClient,
    *,
    upload_url: str,
    media: Dict[str, Any],
    chunk_plan: Dict[str, int],
) -> None:
    total = int(media["size_bytes"])
    chunk_size = int(chunk_plan["chunk_size"])
    count = int(chunk_plan["total_chunk_count"])
    with open(media["path"], "rb") as handle:
        offset = 0
        for index in range(count):
            if index == count - 1:
                length = total - offset
            else:
                length = chunk_size
            data = handle.read(length)
            if len(data) != length:
                raise ProviderError("Local video ended before the expected byte range", provider="tiktok", status_code=500)
            end = offset + length - 1
            response = await client.put(
                upload_url,
                headers={
                    "Content-Type": str(media["mime_type"]),
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
                content=data,
            )
            expected = 201 if index == count - 1 else 206
            if response.status_code != expected:
                if response.status_code >= 400:
                    _json(response, "tiktok")
                raise ProviderError(
                    f"TikTok upload returned HTTP {response.status_code}; expected {expected}",
                    provider="tiktok",
                    retryable=response.status_code >= 500,
                    detail=response.text[:1000],
                )
            offset = end + 1


async def tiktok_status(client: httpx.AsyncClient, access_token: str, publish_id: str) -> Dict[str, Any]:
    response = await client.post(
        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        json={"publish_id": publish_id},
    )
    return _tiktok_json(response)
