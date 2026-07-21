from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.social_publish_config import SocialPublishSettings
from app.social_publish_crypto import SecretBox, SocialSecretError
from app import social_publish_providers as providers
from app import social_publish_store as store
from app.ui_routes import _require_ui_access, _require_user


router = APIRouter()
ALLOWED_MIME_TYPES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}
FINAL_STATUSES = {"PUBLISHED", "FAILED_PERMANENT", "REVOKED"}


class OAuthStartRequest(BaseModel):
    provider: Literal["youtube", "meta", "tiktok"]
    redirect_after: str = "/ui/social/publish"


class PublishRequest(BaseModel):
    account_id: str
    media_id: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    confirmed: bool = False
    music_usage_confirmed: bool = False


class PublicationAdvanceRequest(BaseModel):
    publication_id: str


def _settings() -> SocialPublishSettings:
    settings = SocialPublishSettings.from_env()
    store.init_schema(settings.db_path)
    return settings


def _require_authenticated_user(req: Request):
    _require_ui_access(req)
    user = _require_user(req)
    if user is None:
        raise HTTPException(
            status_code=409,
            detail="Social account connections and publishing require USER_AUTH_ENABLED=true and a signed-in user.",
        )
    return user


def _secret_box(settings: SocialPublishSettings) -> SecretBox:
    try:
        return SecretBox.from_key(settings.token_encryption_key)
    except SocialSecretError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _require_provider_ready(settings: SocialPublishSettings, provider: str) -> None:
    missing = settings.provider_missing(provider)
    if missing:
        raise HTTPException(
            status_code=503,
            detail={"error": "social_provider_not_configured", "provider": provider, "missing": missing},
        )


def _safe_redirect_after(value: str) -> str:
    path = (value or "").strip()
    if not path.startswith("/ui/") or path.startswith("//"):
        return "/ui/social/publish"
    return path[:500]


def _provider_client(settings: SocialPublishSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=float(settings.http_timeout_sec), follow_redirects=True)


def _scope_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[\s,]+", value) if item.strip()]
    return list(fallback)


def _decrypt_account(settings: SocialPublishSettings, account: Dict[str, Any]) -> Dict[str, Any]:
    box = _secret_box(settings)
    result = dict(account)
    result["access_token"] = box.decrypt(str(account.get("access_token_enc") or ""))
    encrypted_refresh = account.get("refresh_token_enc")
    result["refresh_token"] = box.decrypt(str(encrypted_refresh)) if encrypted_refresh else None
    return result


async def _fresh_account(
    client: httpx.AsyncClient,
    settings: SocialPublishSettings,
    *,
    user_id: int,
    account_id: str,
) -> Dict[str, Any]:
    raw = store.get_account(settings.db_path, user_id=user_id, account_id=account_id, include_secrets=True)
    if raw is None or raw.get("revoked_ts") is not None:
        raise HTTPException(status_code=404, detail="connected social account not found")
    account = _decrypt_account(settings, raw)
    expires_ts = account.get("access_expires_ts")
    if expires_ts is None or int(expires_ts) > int(time.time()) + 120:
        return account
    box = _secret_box(settings)
    provider = str(account["provider"])
    refresh_token = account.get("refresh_token")
    if provider == "youtube" and refresh_token:
        refreshed = await providers.refresh_google_token(client, settings, refresh_token)
    elif provider == "tiktok" and refresh_token:
        refreshed = await providers.refresh_tiktok_token(client, settings, refresh_token)
    else:
        raise HTTPException(status_code=401, detail=f"{provider} access token expired; reconnect the account")
    access_token = str(refreshed.get("access_token") or "")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"{provider} token refresh returned no access token")
    new_refresh = str(refreshed.get("refresh_token") or "") or refresh_token
    scopes = _scope_list(refreshed.get("scope"), account.get("scopes") or [])
    store.update_account_tokens(
        settings.db_path,
        user_id=user_id,
        account_id=account_id,
        access_token_enc=box.encrypt(access_token),
        refresh_token_enc=box.encrypt(new_refresh) if new_refresh else None,
        access_expires_ts=refreshed.get("access_expires_ts"),
        refresh_expires_ts=refreshed.get("refresh_expires_ts"),
        scopes=scopes,
    )
    updated = store.get_account(settings.db_path, user_id=user_id, account_id=account_id, include_secrets=True)
    if updated is None:
        raise HTTPException(status_code=500, detail="failed to reload refreshed social account")
    return _decrypt_account(settings, updated)


def _ffprobe(path: str) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
                "-of",
                "json",
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
    except Exception as exc:
        return {"probe_error": f"{type(exc).__name__}: {exc}"}
    metadata: Dict[str, Any] = {}
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    try:
        metadata["duration_sec"] = round(float(format_data.get("duration")), 3)
    except Exception:
        pass
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
    if isinstance(video, dict):
        for key in ("codec_name", "width", "height", "r_frame_rate"):
            if video.get(key) is not None:
                metadata[key] = video[key]
    return metadata


def _signed_media_url(settings: SocialPublishSettings, box: SecretBox, media: Dict[str, Any]) -> str:
    if not settings.public_base_url.startswith("https://"):
        raise HTTPException(
            status_code=503,
            detail="Instagram publishing requires SOCIAL_PUBLIC_BASE_URL to be a publicly reachable HTTPS origin.",
        )
    expires_ts = min(int(media["expires_ts"]), int(time.time()) + 60 * 60)
    signature = box.sign_media(str(media["id"]), expires_ts)
    return f"{settings.public_base_url}/social-media/{quote(str(media['id']))}?expires={expires_ts}&sig={quote(signature)}"


def _idempotency_key(body: PublishRequest, provider: str) -> str:
    supplied = (body.idempotency_key or "").strip()
    if supplied:
        return supplied[:180]
    payload = json.dumps(
        {
            "provider": provider,
            "account_id": body.account_id,
            "media_id": body.media_id,
            "metadata": body.metadata,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provider_failure_status(exc: providers.ProviderError) -> str:
    return "FAILED_RETRYABLE" if exc.retryable else "FAILED_PERMANENT"


@router.get("/ui/social/publish", include_in_schema=False)
async def social_publish_ui(req: Request):
    _require_ui_access(req)
    _require_user(req)
    path = Path(__file__).resolve().parent / "static" / "social_publish.html"
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/ui/api/social/publishing/config")
async def social_publish_config(req: Request):
    _require_authenticated_user(req)
    settings = _settings()
    return {
        "enabled": settings.enabled,
        "readiness": settings.readiness(),
        "media_max_bytes": settings.media_max_bytes,
        "media_ttl_sec": settings.media_ttl_sec,
    }


@router.post("/ui/api/social/oauth/start")
async def social_oauth_start(req: Request, body: OAuthStartRequest):
    user = _require_authenticated_user(req)
    settings = _settings()
    _require_provider_ready(settings, body.provider)
    state = store.create_oauth_state(
        settings.db_path,
        user_id=user.id,
        provider=body.provider,
        redirect_after=_safe_redirect_after(body.redirect_after),
        ttl_sec=settings.oauth_state_ttl_sec,
    )
    if body.provider == "youtube":
        url = providers.build_google_authorize_url(settings, state)
    elif body.provider == "meta":
        url = providers.build_meta_authorize_url(settings, state)
    else:
        url = providers.build_tiktok_authorize_url(settings, state)
    return {"provider": body.provider, "authorization_url": url}


@router.get("/ui/social/oauth/{provider}/callback", include_in_schema=False)
async def social_oauth_callback(
    req: Request,
    provider: Literal["youtube", "meta", "tiktok"],
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    user = _require_authenticated_user(req)
    settings = _settings()
    _require_provider_ready(settings, provider)
    state_record = store.consume_oauth_state(settings.db_path, state=state, provider=provider)
    if state_record is None or int(state_record["user_id"]) != int(user.id):
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    redirect_after = _safe_redirect_after(str(state_record.get("redirect_after") or ""))
    if error:
        return RedirectResponse(
            f"{redirect_after}?oauth_provider={quote(provider)}&oauth_error={quote(error_description or error)}",
            status_code=303,
        )
    if not code:
        raise HTTPException(status_code=400, detail="OAuth callback did not include a code")

    box = _secret_box(settings)
    async with _provider_client(settings) as client:
        if provider == "youtube":
            token = await providers.exchange_google_code(client, settings, code)
            access_token = str(token.get("access_token") or "")
            discovered = await providers.discover_youtube_accounts(client, access_token)
            scopes = _scope_list(token.get("scope"), settings.google_scopes)
        elif provider == "meta":
            token = await providers.exchange_meta_code(client, settings, code)
            access_token = str(token.get("access_token") or "")
            discovered = await providers.discover_meta_accounts(client, settings, access_token)
            scopes = list(settings.meta_scopes)
        else:
            token = await providers.exchange_tiktok_code(client, settings, code)
            access_token = str(token.get("access_token") or "")
            discovered = await providers.discover_tiktok_account(client, access_token, str(token.get("open_id") or ""))
            scopes = _scope_list(token.get("scope"), settings.tiktok_scopes)

    if not access_token or not discovered:
        raise HTTPException(status_code=502, detail=f"{provider} authorization returned no publishable accounts")
    refresh_token = str(token.get("refresh_token") or "") or None
    for item in discovered:
        account_token = str(item.get("access_token") or access_token)
        store.upsert_account(
            settings.db_path,
            user_id=user.id,
            provider=str(item["provider"]),
            external_account_id=str(item["external_account_id"]),
            display_name=str(item["display_name"]),
            account_type=str(item["account_type"]),
            scopes=scopes,
            access_token_enc=box.encrypt(account_token),
            refresh_token_enc=box.encrypt(refresh_token) if refresh_token else None,
            token_type=str(token.get("token_type") or "Bearer"),
            access_expires_ts=token.get("access_expires_ts"),
            refresh_expires_ts=token.get("refresh_expires_ts"),
            metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        )
    return RedirectResponse(
        f"{redirect_after}?oauth_provider={quote(provider)}&oauth_connected={len(discovered)}",
        status_code=303,
    )


@router.get("/ui/api/social/accounts")
async def social_accounts(req: Request):
    user = _require_authenticated_user(req)
    settings = _settings()
    return {"accounts": store.list_accounts(settings.db_path, user_id=user.id)}


@router.delete("/ui/api/social/accounts/{account_id}")
async def social_disconnect_account(req: Request, account_id: str):
    user = _require_authenticated_user(req)
    settings = _settings()
    raw = store.get_account(settings.db_path, user_id=user.id, account_id=account_id, include_secrets=True)
    if raw is None:
        raise HTTPException(status_code=404, detail="connected account not found")
    account = _decrypt_account(settings, raw)
    if account["provider"] in {"youtube", "tiktok"}:
        try:
            async with _provider_client(settings) as client:
                await providers.revoke_provider_token(client, settings, str(account["provider"]), str(account["access_token"]))
        except Exception:
            pass
    changed = store.revoke_account(settings.db_path, user_id=user.id, account_id=account_id)
    return {"revoked": changed}


@router.get("/ui/api/social/accounts/{account_id}/capabilities")
async def social_account_capabilities(req: Request, account_id: str):
    user = _require_authenticated_user(req)
    settings = _settings()
    async with _provider_client(settings) as client:
        account = await _fresh_account(client, settings, user_id=user.id, account_id=account_id)
        if account["provider"] == "tiktok":
            return await providers.tiktok_creator_info(client, str(account["access_token"]))
        return {
            "provider": account["provider"],
            "external_account_id": account["external_account_id"],
            "display_name": account["display_name"],
            "metadata": account.get("metadata") or {},
        }


@router.post("/ui/api/social/media")
async def social_upload_media(req: Request, file: UploadFile = File(...)):
    user = _require_authenticated_user(req)
    settings = _settings()
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="SOCIAL_PUBLISHING_ENABLED is false")
    mime_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail={"error": "unsupported_video_type", "allowed": sorted(ALLOWED_MIME_TYPES)})
    user_dir = Path(settings.media_dir) / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    temp_path = user_dir / f"upload-{time.time_ns()}{ALLOWED_MIME_TYPES[mime_type]}"
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.media_max_bytes:
                    raise HTTPException(status_code=413, detail="video exceeds SOCIAL_MEDIA_MAX_BYTES")
                digest.update(chunk)
                handle.write(chunk)
        if size <= 0:
            raise HTTPException(status_code=400, detail="uploaded video is empty")
        metadata = _ffprobe(str(temp_path))
        asset = store.create_media_asset(
            settings.db_path,
            user_id=user.id,
            path=str(temp_path),
            filename=Path(file.filename or temp_path.name).name[:255],
            mime_type=mime_type,
            size_bytes=size,
            sha256=digest.hexdigest(),
            metadata=metadata,
            ttl_sec=settings.media_ttl_sec,
        )
        return {"media": asset}
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        await file.close()


@router.get("/ui/api/social/media")
async def social_media(req: Request):
    user = _require_authenticated_user(req)
    settings = _settings()
    return {"media": store.list_media_assets(settings.db_path, user_id=user.id)}


@router.get("/social-media/{media_id}", include_in_schema=False)
async def social_public_media(media_id: str, expires: int, sig: str):
    settings = _settings()
    box = _secret_box(settings)
    now = int(time.time())
    if int(expires) < now or int(expires) > now + 60 * 60 + 60:
        raise HTTPException(status_code=403, detail="signed media URL expired")
    if not box.verify_media(media_id, int(expires), sig):
        raise HTTPException(status_code=403, detail="invalid media signature")
    media = store.get_public_media_asset(settings.db_path, media_id=media_id)
    if media is None or int(media["expires_ts"]) < now:
        raise HTTPException(status_code=404, detail="media not found")
    path = Path(str(media["path"]))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media file not found")
    return FileResponse(path, media_type=str(media["mime_type"]), filename=str(media["filename"]))


@router.post("/ui/api/social/publications")
async def social_publish(req: Request, body: PublishRequest):
    user = _require_authenticated_user(req)
    settings = _settings()
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="SOCIAL_PUBLISHING_ENABLED is false")
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Explicit publication confirmation is required")
    media = store.get_media_asset(settings.db_path, user_id=user.id, media_id=body.media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="media asset not found")
    raw_account = store.get_account(settings.db_path, user_id=user.id, account_id=body.account_id, include_secrets=False)
    if raw_account is None:
        raise HTTPException(status_code=404, detail="connected account not found")
    provider = str(raw_account["provider"])
    if provider == "tiktok" and not body.music_usage_confirmed:
        raise HTTPException(status_code=400, detail="TikTok Music Usage Confirmation is required")
    publication = store.create_or_get_publication(
        settings.db_path,
        user_id=user.id,
        provider=provider,
        account_id=body.account_id,
        media_id=body.media_id,
        idempotency_key=_idempotency_key(body, provider),
        request_payload={
            "metadata": body.metadata,
            "confirmed": body.confirmed,
            "music_usage_confirmed": body.music_usage_confirmed,
        },
        consent_ts=int(time.time()),
    )
    if publication["status"] not in {"READY", "FAILED_RETRYABLE"}:
        return {"publication": publication, "idempotent_replay": True}

    box = _secret_box(settings)
    try:
        async with _provider_client(settings) as client:
            account = await _fresh_account(client, settings, user_id=user.id, account_id=body.account_id)
            access_token = str(account["access_token"])
            if provider == "youtube":
                upload_url = await providers.youtube_begin_upload(
                    client, access_token=access_token, media=media, metadata=body.metadata
                )
                store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="UPLOADING",
                    session_secret_enc=box.encrypt(upload_url),
                )
                response = await providers.youtube_upload_file(
                    client, access_token=access_token, upload_url=upload_url, media=media
                )
                remote_id = str(response.get("id") or "")
                if not remote_id:
                    raise providers.ProviderError("YouTube upload returned no video ID", provider="youtube", detail=response)
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="PROCESSING",
                    response=response,
                    remote_id=remote_id,
                )
            elif provider == "facebook":
                page_id = str(account["external_account_id"])
                start = await providers.facebook_begin_reel(
                    client, settings, page_id=page_id, page_token=access_token
                )
                remote_id = str(start.get("video_id") or start.get("id") or "")
                upload_url = str(start.get("upload_url") or "") or None
                if not remote_id:
                    raise providers.ProviderError("Facebook returned no video ID", provider="facebook", detail=start)
                store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="UPLOADING",
                    remote_id=remote_id,
                    remote_context={"start": {k: v for k, v in start.items() if k != "upload_url"}},
                    session_secret_enc=box.encrypt(upload_url) if upload_url else None,
                )
                upload_response = await providers.facebook_upload_reel(
                    client,
                    settings,
                    page_token=access_token,
                    video_id=remote_id,
                    upload_url=upload_url,
                    media=media,
                )
                finish = await providers.facebook_finish_reel(
                    client,
                    settings,
                    page_id=page_id,
                    page_token=access_token,
                    video_id=remote_id,
                    metadata=body.metadata,
                )
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="PROCESSING",
                    response={"upload": upload_response, "finish": finish},
                    remote_id=remote_id,
                )
            elif provider == "instagram":
                signed_url = _signed_media_url(settings, box, media)
                created = await providers.instagram_create_reel_container(
                    client,
                    settings,
                    ig_user_id=str(account["external_account_id"]),
                    page_token=access_token,
                    video_url=signed_url,
                    metadata=body.metadata,
                )
                creation_id = str(created.get("id") or "")
                if not creation_id:
                    raise providers.ProviderError("Instagram returned no creation ID", provider="instagram", detail=created)
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="PROCESSING",
                    response=created,
                    remote_id=creation_id,
                    session_secret_enc=box.encrypt(signed_url),
                )
            elif provider == "tiktok":
                creator = await providers.tiktok_creator_info(client, access_token)
                providers.validate_tiktok_post(body.metadata, creator, media)
                start = await providers.tiktok_begin_post(
                    client, access_token=access_token, media=media, metadata=body.metadata
                )
                data = start.get("data") if isinstance(start.get("data"), dict) else {}
                remote_id = str(data.get("publish_id") or "")
                upload_url = str(data.get("upload_url") or "")
                if not remote_id or not upload_url:
                    raise providers.ProviderError("TikTok returned an incomplete upload session", provider="tiktok", detail=start)
                chunk_plan = start.get("chunk_plan") if isinstance(start.get("chunk_plan"), dict) else {}
                store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="UPLOADING",
                    remote_id=remote_id,
                    remote_context={"chunk_plan": chunk_plan, "creator_info": creator.get("data") or {}},
                    session_secret_enc=box.encrypt(upload_url),
                )
                await providers.tiktok_upload_file(
                    client,
                    upload_url=upload_url,
                    media=media,
                    chunk_plan=chunk_plan,
                )
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status="PROCESSING",
                    response={"publish_id": remote_id},
                    remote_id=remote_id,
                )
            else:
                raise HTTPException(status_code=400, detail=f"unsupported connected account provider: {provider}")
    except providers.ProviderError as exc:
        publication = store.update_publication(
            settings.db_path,
            user_id=user.id,
            publication_id=publication["id"],
            status=_provider_failure_status(exc),
            error=exc.payload(),
        )
        raise HTTPException(status_code=exc.status_code, detail={"publication": publication, "provider_error": exc.payload()}) from exc
    return {"publication": publication, "idempotent_replay": False}


@router.post("/ui/api/social/publications/advance")
async def social_advance_publication(req: Request, body: PublicationAdvanceRequest):
    user = _require_authenticated_user(req)
    settings = _settings()
    publication = store.get_publication(
        settings.db_path, user_id=user.id, publication_id=body.publication_id, include_secret=False
    )
    if publication is None:
        raise HTTPException(status_code=404, detail="publication not found")
    if publication["status"] in FINAL_STATUSES:
        return {"publication": publication}
    try:
        async with _provider_client(settings) as client:
            account = await _fresh_account(client, settings, user_id=user.id, account_id=publication["account_id"])
            access_token = str(account["access_token"])
            provider = str(publication["provider"])
            remote_id = str(publication.get("remote_id") or "")
            if provider == "youtube":
                response = await providers.youtube_status(client, access_token, remote_id)
                items = response.get("items") if isinstance(response.get("items"), list) else []
                item = items[0] if items and isinstance(items[0], dict) else {}
                details = item.get("processingDetails") if isinstance(item.get("processingDetails"), dict) else {}
                state = str(details.get("processingStatus") or "processing").lower()
                status = "PUBLISHED" if state == "succeeded" else "FAILED_PERMANENT" if state == "failed" else "PROCESSING"
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status=status,
                    response=response,
                    error={"processingDetails": details} if status == "FAILED_PERMANENT" else {},
                )
            elif provider == "facebook":
                response = await providers.facebook_status(
                    client, settings, page_token=access_token, video_id=remote_id
                )
                status_payload = response.get("status") if isinstance(response.get("status"), dict) else {}
                state = str(
                    status_payload.get("video_status")
                    or status_payload.get("processing_phase", {}).get("status")
                    or "processing"
                ).lower()
                if state in {"ready", "published", "complete", "completed"}:
                    status = "PUBLISHED"
                elif state in {"error", "failed"}:
                    status = "FAILED_PERMANENT"
                else:
                    status = "PROCESSING"
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status=status,
                    response=response,
                    error=response if status == "FAILED_PERMANENT" else {},
                )
            elif provider == "instagram":
                response = await providers.instagram_container_status(
                    client, settings, page_token=access_token, creation_id=remote_id
                )
                state = str(response.get("status_code") or "IN_PROGRESS").upper()
                if state == "FINISHED":
                    published = await providers.instagram_publish_container(
                        client,
                        settings,
                        ig_user_id=str(account["external_account_id"]),
                        page_token=access_token,
                        creation_id=remote_id,
                    )
                    published_id = str(published.get("id") or "")
                    publication = store.update_publication(
                        settings.db_path,
                        user_id=user.id,
                        publication_id=publication["id"],
                        status="PUBLISHED",
                        response={"container": response, "published": published},
                        remote_context={"creation_id": remote_id, "published_media_id": published_id},
                    )
                elif state in {"ERROR", "EXPIRED"}:
                    publication = store.update_publication(
                        settings.db_path,
                        user_id=user.id,
                        publication_id=publication["id"],
                        status="FAILED_PERMANENT",
                        response=response,
                        error=response,
                    )
                else:
                    publication = store.update_publication(
                        settings.db_path,
                        user_id=user.id,
                        publication_id=publication["id"],
                        status="PROCESSING",
                        response=response,
                    )
            elif provider == "tiktok":
                response = await providers.tiktok_status(client, access_token, remote_id)
                data = response.get("data") if isinstance(response.get("data"), dict) else {}
                state = str(data.get("status") or "PROCESSING").upper()
                if state in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}:
                    status = "PUBLISHED"
                elif state in {"FAILED", "PUBLISH_FAILED"}:
                    status = "FAILED_PERMANENT"
                else:
                    status = "PROCESSING"
                publication = store.update_publication(
                    settings.db_path,
                    user_id=user.id,
                    publication_id=publication["id"],
                    status=status,
                    response=response,
                    error=response if status == "FAILED_PERMANENT" else {},
                )
            else:
                raise HTTPException(status_code=400, detail=f"unsupported publication provider: {provider}")
    except providers.ProviderError as exc:
        publication = store.update_publication(
            settings.db_path,
            user_id=user.id,
            publication_id=publication["id"],
            status=_provider_failure_status(exc),
            error=exc.payload(),
        )
        raise HTTPException(status_code=exc.status_code, detail={"publication": publication, "provider_error": exc.payload()}) from exc
    return {"publication": publication}


@router.get("/ui/api/social/publications")
async def social_publications(req: Request):
    user = _require_authenticated_user(req)
    settings = _settings()
    return {"publications": store.list_publications(settings.db_path, user_id=user.id)}
