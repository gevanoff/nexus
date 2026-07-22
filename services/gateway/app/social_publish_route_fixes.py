from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app import social_publish_routes as base


router = APIRouter()

# Reuse the provider publishing router from Phases 2–4, replacing only the
# endpoints corrected after PR #43 merged. Keeping this composition isolated
# avoids duplicating every OAuth, media, and publication route while making the
# corrected handlers the only mounted handlers for their paths.
_REPLACED_ROUTES = {
    ("/ui/api/social/publishing/config", "GET"),
    ("/ui/api/social/publications/advance", "POST"),
}
for route in base.router.routes:
    path = getattr(route, "path", "")
    methods = set(getattr(route, "methods", set()) or set())
    if any(path == replaced_path and method in methods for replaced_path, method in _REPLACED_ROUTES):
        continue
    router.routes.append(route)


@router.get("/ui/api/social/publishing/config")
async def social_publish_config(req: Request):
    """Describe always-on assisted publishing and opt-in direct publishing."""

    # This response contains deployment capability metadata only. Requiring an
    # authenticated user here would make the always-available assisted page
    # depend on user auth even when every direct provider feature is disabled.
    base._require_ui_access(req)
    settings = base._settings()
    return {
        "assisted_publishing_available": True,
        "direct_publishing_enabled": settings.direct_publishing_enabled,
        # Compatibility for older clients. This field has always represented the
        # provider API feature, not the drafting/assisted workflow.
        "enabled": settings.direct_publishing_enabled,
        "readiness": settings.readiness(),
        "media_max_bytes": settings.media_max_bytes,
        "media_ttl_sec": settings.media_ttl_sec,
    }


def tiktok_publication_status(provider_state: Any) -> str:
    """Map TikTok's provider state without treating inbox handoff as published."""

    state = str(provider_state or "PROCESSING").strip().upper()
    if state == "PUBLISH_COMPLETE":
        return "PUBLISHED"
    if state == "SEND_TO_USER_INBOX":
        return "AWAITING_USER_ACTION"
    if state in {"FAILED", "PUBLISH_FAILED"}:
        return "FAILED_PERMANENT"
    return "PROCESSING"


@router.post("/ui/api/social/publications/advance")
async def social_advance_publication(req: Request, body: base.PublicationAdvanceRequest):
    """Advance a publication, preserving TikTok inbox handoff as non-final."""

    user = base._require_authenticated_user(req)
    settings = base._settings()
    publication = base.store.get_publication(
        settings.db_path,
        user_id=user.id,
        publication_id=body.publication_id,
        include_secret=False,
    )
    if publication is None:
        raise HTTPException(status_code=404, detail="publication not found")
    if publication["status"] in base.FINAL_STATUSES:
        return {"publication": publication}

    # The merged implementation remains authoritative for YouTube, Facebook,
    # and Instagram. Only TikTok's SEND_TO_USER_INBOX interpretation changes.
    if str(publication.get("provider") or "") != "tiktok":
        return await base.social_advance_publication(req, body)

    try:
        async with base._provider_client(settings) as client:
            account = await base._fresh_account(
                client,
                settings,
                user_id=user.id,
                account_id=publication["account_id"],
            )
            access_token = str(account["access_token"])
            remote_id = str(publication.get("remote_id") or "")
            response = await base.providers.tiktok_status(client, access_token, remote_id)
            data: Dict[str, Any] = response.get("data") if isinstance(response.get("data"), dict) else {}
            status = tiktok_publication_status(data.get("status"))
            publication = base.store.update_publication(
                settings.db_path,
                user_id=user.id,
                publication_id=publication["id"],
                status=status,
                response=response,
                error=response if status == "FAILED_PERMANENT" else {},
            )
    except base.providers.ProviderError as exc:
        publication = base.store.update_publication(
            settings.db_path,
            user_id=user.id,
            publication_id=publication["id"],
            status=base._provider_failure_status(exc),
            error=exc.payload(),
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"publication": publication, "provider_error": exc.payload()},
        ) from exc

    return {"publication": publication}
