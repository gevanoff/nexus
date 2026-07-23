from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app import social_routes as base


router = APIRouter()

# Reuse the existing Social Studio routes, replacing only platform draft
# generation. Keeping the correction isolated avoids duplicating state, prompt,
# and field-generation behavior while the drafting API is hardened.
_REPLACED_ROUTES = {("/ui/api/social/generate", "POST")}
for route in base.router.routes:
    path = getattr(route, "path", "")
    methods = set(getattr(route, "methods", set()) or set())
    if any(path == replaced_path and method in methods for replaced_path, method in _REPLACED_ROUTES):
        continue
    router.routes.append(route)


def _completion_finish_reason(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or "").strip().lower()


def _json_object_appears_incomplete(text: str) -> bool:
    """Detect an unterminated top-level JSON object without guessing repairs."""

    depth = 0
    seen_object = False
    in_string = False
    escaped = False
    for char in str(text or ""):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            seen_object = True
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
    return seen_object and (depth > 0 or in_string)


def _merge_unique(target: List[str], values: Any) -> None:
    seen = {str(item).casefold() for item in target}
    for item in values if isinstance(values, list) else []:
        text = base._text(item, limit=1500)
        key = text.casefold()
        if text and key not in seen:
            target.append(text)
            seen.add(key)


def _merge_shared(target: Dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    if not target.get("content_summary"):
        target["content_summary"] = base._text(source.get("content_summary"), limit=4000)
    assumptions = target.setdefault("factual_assumptions", [])
    _merge_unique(assumptions, source.get("factual_assumptions"))


def _merge_usage(target: Dict[str, int], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        target[key] = target.get(key, 0) + int(value)


def _generation_error(
    *,
    platform: str,
    raw_text: str,
    finish_reason: str,
    parse_error: Exception | None = None,
) -> HTTPException:
    truncated = finish_reason in {"length", "max_tokens"} or _json_object_appears_incomplete(raw_text)
    if truncated:
        return HTTPException(
            status_code=502,
            detail={
                "error": "social_draft_truncated",
                "message": (
                    f"The {platform} draft reached the model output limit before completing its JSON. "
                    "Use fewer variants or select a model with a larger response budget."
                ),
                "platform": platform,
                "finish_reason": finish_reason or "unknown",
                "raw_text": raw_text[:8000],
            },
        )
    return HTTPException(
        status_code=502,
        detail={
            "error": "invalid_social_draft_json",
            "message": str(parse_error or "model did not return valid JSON"),
            "platform": platform,
            "finish_reason": finish_reason or "unknown",
            "raw_text": raw_text[:8000],
        },
    )


async def _call_platform_once(
    req: Request,
    body: base.SocialPromptRequest,
    platform: str,
) -> Dict[str, Any]:
    platform_body = body.model_copy(update={"platforms": [platform]})
    prompt = base.build_social_prompt(platform_body)
    messages = [
        base.ChatMessage(role="system", content=prompt["system_prompt"]),
        base.ChatMessage(role="user", content=prompt["user_prompt"]),
    ]
    model = base._text(body.model, limit=500) or "default"
    completion = base.ChatCompletionRequest(
        model=model,
        messages=messages,
        temperature=0.35,
        max_tokens=4000,
        stream=False,
    )
    decision = base.decide_route(
        cfg=base.router_cfg(),
        request_model=model,
        headers={str(key).lower(): str(value) for key, value in req.headers.items()},
        messages=[message.model_dump(exclude_none=True) for message in messages],
        has_tools=False,
        enable_policy=False,
    )
    base.check_backend_ready(decision.backend, route_kind="chat")
    await base.check_capability(decision.backend, "chat")
    admission = base.get_admission_controller()
    await admission.acquire(decision.backend, "chat")
    try:
        response = await base.call_backend_chat(
            completion,
            decision.backend,
            decision.model,
            request_id=str(getattr(req.state, "request_id", "") or "") or None,
        )
    finally:
        admission.release(decision.backend, "chat")

    raw_text = base._extract_model_text(response)
    if not raw_text:
        raise HTTPException(status_code=502, detail="model returned no text")
    finish_reason = _completion_finish_reason(response)
    if finish_reason in {"length", "max_tokens"}:
        raise _generation_error(platform=platform, raw_text=raw_text, finish_reason=finish_reason)
    try:
        drafts = base.parse_social_drafts(raw_text, [platform], prompt["variants"])
    except ValueError as exc:
        raise _generation_error(
            platform=platform,
            raw_text=raw_text,
            finish_reason=finish_reason,
            parse_error=exc,
        ) from exc

    return {
        "drafts": drafts,
        "prompt": prompt,
        "routing": {
            "requested_model": model,
            "backend": decision.backend,
            "model": decision.model,
            "reason": decision.reason,
            "platform": platform,
            "variants": prompt["variants"],
            "finish_reason": finish_reason or "stop",
        },
        "usage": response.get("usage") if isinstance(response, dict) else None,
    }


def _retryable_generation_error(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return detail.get("error") in {"social_draft_truncated", "invalid_social_draft_json"}


async def _generate_platform(
    req: Request,
    body: base.SocialPromptRequest,
    platform: str,
) -> Dict[str, Any]:
    variants = min(3, max(1, int(body.variants or 1)))
    try:
        return await _call_platform_once(req, body, platform)
    except HTTPException as exc:
        if variants <= 1 or not _retryable_generation_error(exc):
            raise

    # A multi-variant response may exceed a small alias cap. Retry as bounded,
    # independently validated one-variant calls and aggregate them server-side.
    combined = {
        "shared": {"content_summary": "", "factual_assumptions": []},
        "platforms": {platform: {"variants": [], "warnings": []}},
    }
    prompts: List[Dict[str, Any]] = []
    routing_calls: List[Dict[str, Any]] = []
    usage: Dict[str, int] = {}
    original_instruction = base._text(body.custom_instruction, limit=4000)
    for index in range(variants):
        distinction = (
            f"Generate variant {index + 1} of {variants}. Use a distinct editorial angle while preserving all facts."
        )
        instruction = f"{original_instruction}\n\n{distinction}".strip()
        single_body = body.model_copy(
            update={"platforms": [platform], "variants": 1, "custom_instruction": instruction}
        )
        result = await _call_platform_once(req, single_body, platform)
        platform_result = result["drafts"]["platforms"][platform]
        combined["platforms"][platform]["variants"].extend(platform_result["variants"][:1])
        _merge_unique(combined["platforms"][platform]["warnings"], platform_result.get("warnings"))
        _merge_shared(combined["shared"], result["drafts"].get("shared"))
        prompts.append(result["prompt"])
        routing_calls.append(result["routing"])
        _merge_usage(usage, result.get("usage"))

    first_routing = routing_calls[0]
    return {
        "drafts": combined,
        "prompt": prompts[0],
        "routing": {
            **first_routing,
            "variants": variants,
            "strategy": "per_variant_retry",
            "calls": routing_calls,
        },
        "usage": usage or None,
        "generation_prompts": prompts,
    }


@router.post("/ui/api/social/generate")
async def social_generate(req: Request, body: base.SocialPromptRequest):
    base._require_ui_access(req)
    base._require_user(req)
    overall_prompt = base.build_social_prompt(body)
    combined = {
        "shared": {"content_summary": "", "factual_assumptions": []},
        "platforms": {},
    }
    routing_calls: List[Dict[str, Any]] = []
    generation_prompts: Dict[str, Any] = {}
    usage: Dict[str, int] = {}

    for platform in overall_prompt["platforms"]:
        result = await _generate_platform(req, body, platform)
        combined["platforms"][platform] = result["drafts"]["platforms"][platform]
        _merge_shared(combined["shared"], result["drafts"].get("shared"))
        generation_prompts[platform] = result.get("generation_prompts") or [result["prompt"]]
        calls = result["routing"].get("calls")
        if isinstance(calls, list):
            routing_calls.extend(calls)
        else:
            routing_calls.append(result["routing"])
        _merge_usage(usage, result.get("usage"))

    first_routing = routing_calls[0] if routing_calls else {}
    return {
        "drafts": combined,
        "prompt": {
            "system_prompt": overall_prompt["system_prompt"],
            "user_prompt": overall_prompt["user_prompt"],
            "schema": overall_prompt["schema"],
        },
        "generation_prompts": generation_prompts,
        "routing": {
            "requested_model": base._text(body.model, limit=500) or "default",
            "backend": first_routing.get("backend", ""),
            "model": first_routing.get("model", ""),
            "reason": first_routing.get("reason", ""),
            "strategy": "per_platform",
            "calls": routing_calls,
        },
        "usage": usage or None,
    }
