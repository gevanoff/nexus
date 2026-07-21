from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import user_store
from app.backends import check_capability, get_admission_controller
from app.config import S
from app.health_checker import check_backend_ready
from app.models import ChatCompletionRequest, ChatMessage
from app.router import decide_route
from app.router_cfg import router_cfg
from app.ui_routes import _require_ui_access, _require_user
from app.upstreams import call_backend_chat


router = APIRouter()

PLATFORM_KEYS = ("youtube", "facebook", "instagram", "tiktok")
LIST_OUTPUT_FIELDS = {"hashtags", "tags"}
MAX_BRANDS = 25

PLATFORM_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "youtube": {
        "label": "YouTube Shorts",
        "fields": ["title", "description", "hashtags", "tags", "thumbnail_text"],
        "guidance": (
            "Use a clear, search-readable title and a useful description. Put durable context and links in the "
            "description. Hashtags and tags must be relevant rather than generic filler. Thumbnail text must be "
            "brief and work as an optional frame or external cover concept."
        ),
    },
    "facebook": {
        "label": "Facebook Reels",
        "fields": ["title", "description", "hashtags", "link", "thumbnail_text"],
        "guidance": (
            "Write for an audience that may encounter the clip without prior context. The description may be more "
            "explanatory than the short-form captions. Include a supplied link only when it serves the stated call "
            "to action."
        ),
    },
    "instagram": {
        "label": "Instagram Reels",
        "fields": ["caption", "hashtags", "alt_text", "cover_text"],
        "guidance": (
            "Lead with a strong first sentence, then provide concise context. Keep hashtags focused. Alt text must "
            "describe visible content rather than repeat promotional copy. Cover text must remain short."
        ),
    },
    "tiktok": {
        "label": "TikTok",
        "fields": ["caption", "hashtags", "cover_text"],
        "guidance": (
            "Use a direct, search-readable caption that identifies the subject without clickbait. Integrate a small "
            "set of relevant hashtags. Cover text must be short and legible."
        ),
    },
}

GENERIC_SYSTEM_PROMPT = """You create platform-specific publication metadata for short-form videos.

Work only from the supplied brand profile and factual video brief. Do not invent names, dates, locations, quotations, credentials, history, relationships, product capabilities, or event details. When information is uncertain, omit the claim or record it in warnings. Preserve supplied spellings and terminology.

The platform contracts are generic defaults. Brand-specific guidance applies only to the selected brand and must not be generalized into the base rules. A user instruction may refine style or emphasis, but it must not override factual accuracy or the required JSON shape.

Return only one valid JSON object. Do not use Markdown fences or commentary outside the JSON."""

DEFAULT_BRAND: Dict[str, Any] = {
    "id": "generic",
    "name": "Generic / unbranded",
    "description": "",
    "audience": "",
    "voice": "Clear, accurate, and concise.",
    "terminology": [],
    "required_facts": [],
    "prohibited_claims": [],
    "calls_to_action": [],
    "default_links": [],
    "default_hashtags": [],
    "platform_guidance": {key: "" for key in PLATFORM_KEYS},
    "prompt_addendum": "",
}

DEFAULT_STATE: Dict[str, Any] = {
    "version": 1,
    "active_brand_id": "generic",
    "brands": [DEFAULT_BRAND],
    "working_brief": {},
    "global_guidance": "",
}


class SocialPromptRequest(BaseModel):
    model: str = "default"
    brand: Dict[str, Any] = Field(default_factory=dict)
    brief: Dict[str, Any] = Field(default_factory=dict)
    platforms: List[Literal["youtube", "facebook", "instagram", "tiktok"]] = Field(
        default_factory=lambda: list(PLATFORM_KEYS)
    )
    variants: int = 1
    custom_instruction: str = ""


class SocialStateRequest(BaseModel):
    state: Dict[str, Any] = Field(default_factory=dict)


def _text(value: Any, *, limit: int = 8000) -> str:
    if value is None:
        return ""
    value = value if isinstance(value, str) else str(value)
    return value.strip()[:limit]


def _string_list(value: Any, *, limit: int = 40, item_limit: int = 500) -> List[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    out: List[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = _text(item, limit=item_limit)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _slug(value: Any, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _text(value, limit=80).lower()).strip("-")
    return slug[:64] or fallback


def normalize_brand(raw: Any, *, fallback_id: str = "brand") -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    brand_id = _slug(source.get("id") or source.get("name"), fallback=fallback_id)
    platform_guidance = source.get("platform_guidance")
    if not isinstance(platform_guidance, dict):
        platform_guidance = {}
    return {
        "id": brand_id,
        "name": _text(source.get("name"), limit=160) or "Untitled brand",
        "description": _text(source.get("description"), limit=3000),
        "audience": _text(source.get("audience"), limit=2000),
        "voice": _text(source.get("voice"), limit=2000),
        "terminology": _string_list(source.get("terminology")),
        "required_facts": _string_list(source.get("required_facts"), item_limit=1000),
        "prohibited_claims": _string_list(source.get("prohibited_claims"), item_limit=1000),
        "calls_to_action": _string_list(source.get("calls_to_action"), item_limit=1000),
        "default_links": _string_list(source.get("default_links"), item_limit=1000),
        "default_hashtags": _string_list(source.get("default_hashtags"), item_limit=120),
        "platform_guidance": {
            key: _text(platform_guidance.get(key), limit=2500) for key in PLATFORM_KEYS
        },
        "prompt_addendum": _text(source.get("prompt_addendum"), limit=4000),
    }


def normalize_brief(raw: Any) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    return {
        "asset_name": _text(source.get("asset_name"), limit=500),
        "subject": _text(source.get("subject"), limit=1000),
        "content_summary": _text(source.get("content_summary"), limit=4000),
        "transcript_notes": _text(source.get("transcript_notes"), limit=30000),
        "key_points": _string_list(source.get("key_points"), limit=60, item_limit=1500),
        "people_organizations": _string_list(source.get("people_organizations"), limit=60, item_limit=500),
        "dates_locations": _string_list(source.get("dates_locations"), limit=60, item_limit=500),
        "content_goal": _text(source.get("content_goal"), limit=1000),
        "audience_override": _text(source.get("audience_override"), limit=1000),
        "call_to_action": _text(source.get("call_to_action"), limit=1500),
        "destination_url": _text(source.get("destination_url"), limit=1500),
        "language": _text(source.get("language"), limit=120) or "English",
        "factual_constraints": _string_list(source.get("factual_constraints"), limit=60, item_limit=1500),
        "extra_notes": _text(source.get("extra_notes"), limit=5000),
    }


def normalize_state(raw: Any) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    raw_brands = source.get("brands") if isinstance(source.get("brands"), list) else []
    brands: List[Dict[str, Any]] = []
    used: set[str] = set()
    for index, item in enumerate(raw_brands[:MAX_BRANDS]):
        brand = normalize_brand(item, fallback_id=f"brand-{index + 1}")
        base_id = brand["id"]
        candidate = base_id
        suffix = 2
        while candidate in used:
            candidate = f"{base_id}-{suffix}"
            suffix += 1
        brand["id"] = candidate
        used.add(candidate)
        brands.append(brand)
    if not brands:
        brands = [normalize_brand(DEFAULT_BRAND, fallback_id="generic")]
    active = _text(source.get("active_brand_id"), limit=80)
    if active not in {brand["id"] for brand in brands}:
        active = brands[0]["id"]
    return {
        "version": 1,
        "active_brand_id": active,
        "brands": brands,
        "working_brief": normalize_brief(source.get("working_brief")),
        "global_guidance": _text(source.get("global_guidance"), limit=4000),
    }


def _selected_platforms(values: Any) -> List[str]:
    out: List[str] = []
    for item in values if isinstance(values, list) else []:
        key = _text(item, limit=40).lower()
        if key in PLATFORM_KEYS and key not in out:
            out.append(key)
    if not out:
        raise HTTPException(status_code=400, detail="select at least one platform")
    return out


def _output_schema(platforms: List[str], variants: int) -> Dict[str, Any]:
    return {
        "shared": {
            "content_summary": "string",
            "factual_assumptions": ["string"],
        },
        "platforms": {
            key: {
                "variants": [
                    {
                        **{field: (["string"] if field in LIST_OUTPUT_FIELDS else "string") for field in PLATFORM_CONTRACTS[key]["fields"]},
                        "rationale": "string",
                    }
                    for _ in range(variants)
                ],
                "warnings": ["string"],
            }
            for key in platforms
        },
    }


def build_social_prompt(request: SocialPromptRequest) -> Dict[str, Any]:
    platforms = _selected_platforms(request.platforms)
    variants = min(3, max(1, int(request.variants or 1)))
    brand = normalize_brand(request.brand or DEFAULT_BRAND, fallback_id="generic")
    brief = normalize_brief(request.brief)
    contracts = {
        key: {
            **PLATFORM_CONTRACTS[key],
            "brand_override": brand["platform_guidance"].get(key) or "",
        }
        for key in platforms
    }
    schema = _output_schema(platforms, variants)
    user_payload = {
        "brand_profile": brand,
        "video_brief": brief,
        "platform_contracts": contracts,
        "variant_count": variants,
        "user_instruction": _text(request.custom_instruction, limit=4000),
        "required_output_schema": schema,
    }
    user_prompt = (
        "Generate publication metadata for the selected platforms. Follow the required output schema exactly. "
        "Each variants array must contain exactly variant_count objects. Hashtags and tags must be JSON arrays "
        "without leading explanation. Use warnings for missing or ambiguous facts.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    return {
        "system_prompt": GENERIC_SYSTEM_PROMPT,
        "user_prompt": user_prompt,
        "schema": schema,
        "brand": brand,
        "brief": brief,
        "platforms": platforms,
        "variants": variants,
    }


def _extract_model_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def parse_social_drafts(text: str, platforms: List[str], variants: int) -> Dict[str, Any]:
    raw = _text(text, limit=100000)
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"model did not return valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("platforms"), dict):
        raise ValueError("model JSON must contain a platforms object")

    normalized: Dict[str, Any] = {
        "shared": payload.get("shared") if isinstance(payload.get("shared"), dict) else {},
        "platforms": {},
    }
    for key in platforms:
        source = payload["platforms"].get(key)
        if not isinstance(source, dict):
            raise ValueError(f"model JSON is missing platform: {key}")
        source_variants = source.get("variants")
        if not isinstance(source_variants, list) or not source_variants:
            raise ValueError(f"platform {key} must contain variants")
        cleaned_variants: List[Dict[str, Any]] = []
        for item in source_variants[:variants]:
            if not isinstance(item, dict):
                continue
            cleaned: Dict[str, Any] = {}
            for field in PLATFORM_CONTRACTS[key]["fields"]:
                if field in LIST_OUTPUT_FIELDS:
                    cleaned[field] = _string_list(item.get(field), limit=40, item_limit=120)
                else:
                    cleaned[field] = _text(item.get(field), limit=10000)
            cleaned["rationale"] = _text(item.get("rationale"), limit=2000)
            cleaned_variants.append(cleaned)
        if not cleaned_variants:
            raise ValueError(f"platform {key} did not contain a usable variant")
        normalized["platforms"][key] = {
            "variants": cleaned_variants,
            "warnings": _string_list(source.get("warnings"), limit=30, item_limit=1000),
        }
    return normalized


def _load_social_state(user: Any) -> Dict[str, Any]:
    if user is None:
        return normalize_state(DEFAULT_STATE)
    settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id) or {}
    return normalize_state(settings.get("social_studio"))


def _save_social_state(user: Any, state: Dict[str, Any]) -> bool:
    if user is None:
        return False
    settings = user_store.get_settings(S.USER_DB_PATH, user_id=user.id) or {}
    settings = dict(settings)
    settings["social_studio"] = state
    user_store.set_settings(S.USER_DB_PATH, user_id=user.id, settings=settings)
    return True


@router.get("/ui/social", include_in_schema=False)
async def social_ui(req: Request):
    _require_ui_access(req)
    _require_user(req)
    path = Path(__file__).resolve().parent / "static" / "social.html"
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/ui/api/social/state")
async def social_state(req: Request):
    _require_ui_access(req)
    user = _require_user(req)
    return {
        "state": _load_social_state(user),
        "persistent": user is not None,
        "platform_contracts": PLATFORM_CONTRACTS,
    }


@router.put("/ui/api/social/state")
async def save_social_state(req: Request, body: SocialStateRequest):
    _require_ui_access(req)
    user = _require_user(req)
    state = normalize_state(body.state)
    persistent = _save_social_state(user, state)
    return {"state": state, "persistent": persistent}


@router.post("/ui/api/social/prompt")
async def social_prompt(req: Request, body: SocialPromptRequest):
    _require_ui_access(req)
    _require_user(req)
    return build_social_prompt(body)


@router.post("/ui/api/social/generate")
async def social_generate(req: Request, body: SocialPromptRequest):
    _require_ui_access(req)
    _require_user(req)
    prompt = build_social_prompt(body)
    messages = [
        ChatMessage(role="system", content=prompt["system_prompt"]),
        ChatMessage(role="user", content=prompt["user_prompt"]),
    ]
    model = _text(body.model, limit=500) or "default"
    completion = ChatCompletionRequest(
        model=model,
        messages=messages,
        temperature=0.35,
        max_tokens=4000,
        stream=False,
    )
    decision = decide_route(
        cfg=router_cfg(),
        request_model=model,
        headers={str(k).lower(): str(v) for k, v in req.headers.items()},
        messages=[message.model_dump(exclude_none=True) for message in messages],
        has_tools=False,
        enable_policy=False,
    )
    check_backend_ready(decision.backend, route_kind="chat")
    await check_capability(decision.backend, "chat")
    admission = get_admission_controller()
    await admission.acquire(decision.backend, "chat")
    try:
        response = await call_backend_chat(
            completion,
            decision.backend,
            decision.model,
            request_id=str(getattr(req.state, "request_id", "") or "") or None,
        )
    finally:
        admission.release(decision.backend, "chat")

    raw_text = _extract_model_text(response)
    if not raw_text:
        raise HTTPException(status_code=502, detail="model returned no text")
    try:
        drafts = parse_social_drafts(raw_text, prompt["platforms"], prompt["variants"])
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "invalid_social_draft_json",
                "message": str(exc),
                "raw_text": raw_text[:8000],
            },
        ) from exc

    return {
        "drafts": drafts,
        "prompt": {
            "system_prompt": prompt["system_prompt"],
            "user_prompt": prompt["user_prompt"],
            "schema": prompt["schema"],
        },
        "routing": {
            "requested_model": model,
            "backend": decision.backend,
            "model": decision.model,
            "reason": decision.reason,
        },
        "usage": response.get("usage") if isinstance(response, dict) else None,
    }
