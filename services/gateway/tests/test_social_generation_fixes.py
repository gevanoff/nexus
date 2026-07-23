from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import social_route_fixes as fixes
from app.social_routes import SocialPromptRequest


ROOT = Path(__file__).resolve().parents[3]


def test_social_generate_route_is_replaced_exactly_once():
    matches = []
    for route in fixes.router.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        if path == "/ui/api/social/generate" and "POST" in methods:
            matches.append(route)

    assert len(matches) == 1
    assert matches[0].endpoint is fixes.social_generate


def test_finish_reason_and_incomplete_json_detection():
    assert fixes._completion_finish_reason({"choices": [{"finish_reason": "length"}]}) == "length"
    assert fixes._completion_finish_reason({"choices": [{"finish_reason": "stop"}]}) == "stop"
    assert fixes._json_object_appears_incomplete('{"platforms":{"youtube":') is True
    assert fixes._json_object_appears_incomplete('{"text":"a } brace"}') is False
    assert fixes._json_object_appears_incomplete('{"text":"unterminated') is True


def test_interactive_alias_caps_support_structured_outputs():
    payload = json.loads((ROOT / "services/gateway/app/model_aliases.json").read_text(encoding="utf-8"))
    aliases = payload["aliases"]

    assert aliases["fast"]["max_tokens_cap"] == 2048
    assert aliases["fast-reasoning"]["max_tokens_cap"] == 2048
    assert aliases["default"]["max_tokens_cap"] == 4096
    assert aliases["reasoning"]["max_tokens_cap"] == 4096


@pytest.mark.asyncio
async def test_multi_variant_generation_retries_as_single_variants(monkeypatch):
    calls = []

    async def fake_call(_req, body, platform):
        calls.append(body.variants)
        if len(calls) == 1:
            raise HTTPException(
                status_code=502,
                detail={"error": "social_draft_truncated", "platform": platform},
            )
        variant_number = len(calls) - 1
        return {
            "drafts": {
                "shared": {
                    "content_summary": "A stable summary.",
                    "factual_assumptions": ["No unsupported claims."],
                },
                "platforms": {
                    platform: {
                        "variants": [{"title": f"Variant {variant_number}"}],
                        "warnings": [],
                    }
                },
            },
            "prompt": {"system_prompt": "system", "user_prompt": "user", "schema": {}},
            "routing": {
                "requested_model": body.model,
                "backend": "local_vllm_fast",
                "model": "example-model",
                "reason": "test",
                "platform": platform,
                "variants": 1,
                "finish_reason": "stop",
            },
            "usage": {"completion_tokens": 10},
        }

    monkeypatch.setattr(fixes, "_call_platform_once", fake_call)
    body = SocialPromptRequest.model_validate(
        {
            "model": "fast",
            "brand": {"name": "Example"},
            "brief": {"subject": "Example video"},
            "platforms": ["youtube"],
            "variants": 3,
        }
    )

    result = await fixes._generate_platform(object(), body, "youtube")

    assert calls == [3, 1, 1, 1]
    variants = result["drafts"]["platforms"]["youtube"]["variants"]
    assert [item["title"] for item in variants] == ["Variant 1", "Variant 2", "Variant 3"]
    assert result["routing"]["strategy"] == "per_variant_retry"
    assert result["usage"]["completion_tokens"] == 30


def test_truncation_error_is_distinct_from_malformed_json():
    truncated = fixes._generation_error(
        platform="youtube",
        raw_text='{"platforms": {',
        finish_reason="length",
    )
    malformed = fixes._generation_error(
        platform="youtube",
        raw_text='{"platforms": nope}',
        finish_reason="stop",
        parse_error=ValueError("bad JSON"),
    )

    assert truncated.detail["error"] == "social_draft_truncated"
    assert "output limit" in truncated.detail["message"]
    assert malformed.detail["error"] == "invalid_social_draft_json"
