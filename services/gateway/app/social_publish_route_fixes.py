from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app import social_publish_routes as base


router = APIRouter()

# Reuse the provider publishing router from Phases 2–4, replacing only the
# publication-advance endpoint reviewed after PR #43 merged. Keeping this
# composition isolated avoids duplicating every OAuth, media, and publication
# route while making the corrected endpoint the only mounted handler for the
# path.
for route in base.router.routes:
    methods = set(getattr(route, "methods", set()) or set())
    if getattr(route, "path", "") == "/ui/api/social/publications/advance" and "POST" in methods:
        continue
    router.routes.append(route)


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
