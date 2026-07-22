from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.social_routes import (
    SocialFieldRequest,
    SocialPromptRequest,
    build_social_field_prompt,
    build_social_prompt,
    normalize_state,
    parse_social_drafts,
    parse_social_field_value,
)


def _request(**overrides):
    payload = {
        "brand": {
            "id": "brand_generic",
            "name": "Generic / unbranded",
            "description": "",
            "audience": "",
            "voice": "",
            "terminology": [],
            "required_facts": [],
            "prohibited_claims": [],
            "calls_to_action": [],
            "default_links": [],
            "default_hashtags": [],
            "platform_guidance": {},
            "prompt_addendum": "",
        },
        "brief": {
            "subject": "A short demonstration",
            "content_summary": "A presenter demonstrates one technique.",
            "key_points": ["The movement begins from a stable position."],
        },
        "platforms": ["youtube", "instagram"],
        "variants": 1,
    }
    payload.update(overrides)
    return SocialPromptRequest.model_validate(payload)


def test_generic_prompt_is_brand_neutral():
    prompt = build_social_prompt(_request())
    combined = f"{prompt['system_prompt']}\n{prompt['user_prompt']}"
    assert "Bay Area Shastar Vidiya" not in combined
    assert "Shastar" not in combined
    assert "Generic / unbranded" in combined


def test_brand_guidance_is_scoped_to_selected_brand_context():
    request = _request(
        brand={
            "id": "northwind",
            "name": "Northwind Workshop",
            "description": "Practical repair education.",
            "audience": "Home mechanics",
            "voice": "Clear and direct",
            "terminology": ["Use 'workpiece', not 'item'."],
            "required_facts": ["State when eye protection is required."],
            "prohibited_claims": ["Do not claim a repair is universally safe."],
            "calls_to_action": ["Read the full procedure."],
            "default_links": ["https://example.invalid/procedure"],
            "default_hashtags": ["#NorthwindWorkshop"],
            "platform_guidance": {"youtube": "Prefer descriptive search terms."},
            "prompt_addendum": "Use metric measurements first.",
        }
    )
    prompt = build_social_prompt(request)
    assert "Northwind Workshop" not in prompt["system_prompt"]
    assert "Use metric measurements first." not in prompt["system_prompt"]
    assert "Northwind Workshop" in prompt["user_prompt"]
    assert "Use metric measurements first." in prompt["user_prompt"]


def test_prompt_only_includes_selected_platform_contracts():
    prompt = build_social_prompt(_request(platforms=["tiktok"]))
    assert '"tiktok"' in prompt["user_prompt"]
    assert '"youtube"' not in prompt["user_prompt"]
    assert '"instagram"' not in prompt["user_prompt"]
    assert '"facebook"' not in prompt["user_prompt"]


def test_parse_social_drafts_accepts_fenced_json():
    raw = """```json
    {
      "platforms": {
        "youtube": {
          "variants": [
            {
              "title": "Stable movement explained",
              "description": "A concise explanation.",
              "hashtags": ["#Movement"],
              "tags": ["movement", "instruction"],
              "thumbnail_text": "Stable Start"
            }
          ]
        }
      }
    }
    ```"""
    parsed = parse_social_drafts(raw, platforms=["youtube"], variants=1)
    assert parsed["platforms"]["youtube"]["variants"][0]["title"] == "Stable movement explained"
    assert parsed["platforms"]["youtube"]["variants"][0]["hashtags"] == ["#Movement"]


def test_normalize_state_deduplicates_brand_ids_and_keeps_selection_valid():
    state = normalize_state(
        {
            "brands": [
                {"id": "same", "name": "First"},
                {"id": "same", "name": "Second"},
            ],
            "active_brand_id": "missing",
            "brief": {"subject": "Example"},
        }
    )
    assert len(state["brands"]) == 2
    assert len({brand["id"] for brand in state["brands"]}) == 2
    assert state["active_brand_id"] in {brand["id"] for brand in state["brands"]}


def test_parsed_output_is_json_serializable():
    raw = json.dumps(
        {
            "platforms": {
                "instagram": {
                    "variants": [
                        {
                            "caption": "A short caption.",
                            "hashtags": ["#Example"],
                            "alt_text": "A person demonstrating a movement.",
                            "cover_text": "One Movement",
                        }
                    ]
                }
            }
        }
    )
    parsed = parse_social_drafts(raw, platforms=["instagram"], variants=1)
    json.dumps(parsed)


def test_field_prompt_uses_current_brand_and_brief_context():
    request = SocialFieldRequest.model_validate(
        {
            "model": "default",
            "section": "brief",
            "field": "key_points",
            "brand": {"name": "Northwind Workshop", "audience": "Home mechanics"},
            "brief": {
                "subject": "Replacing a worn belt",
                "content_summary": "The presenter shows the safe replacement sequence.",
                "transcript_notes": "Disconnect power before opening the housing.",
            },
        }
    )

    prompt = build_social_field_prompt(request)

    assert prompt["output"] == "list"
    assert '"target_field": "key_points"' in prompt["user_prompt"]
    assert "Northwind Workshop" in prompt["user_prompt"]
    assert "Disconnect power" in prompt["user_prompt"]
    assert "Complete only the requested target field" in prompt["user_prompt"]


def test_field_prompt_rejects_fields_outside_supported_ranges():
    request = SocialFieldRequest.model_validate(
        {"section": "brief", "field": "transcript_notes", "brand": {}, "brief": {}}
    )

    with pytest.raises(HTTPException) as exc:
        build_social_field_prompt(request)

    assert exc.value.status_code == 400


def test_parse_social_field_value_normalizes_list_output():
    parsed = parse_social_field_value(
        '```json\n{"value":["Stable starting position","Disconnect power","Stable starting position"]}\n```',
        output="list",
    )

    assert parsed == ["Stable starting position", "Disconnect power"]


def test_parse_social_field_value_accepts_string_output():
    parsed = parse_social_field_value('{"value":"Clear, practical, and direct."}', output="string")

    assert parsed == "Clear, practical, and direct."
