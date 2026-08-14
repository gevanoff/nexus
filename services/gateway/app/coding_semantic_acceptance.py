from __future__ import annotations

import json
from typing import Any, Mapping


_REQUIRED_BOOLEAN_FIELDS = (
    "causal_alignment",
    "existing_mechanism_checked",
    "acceptance_criteria_checked",
)


def build_review_messages(
    *,
    original_request: str,
    current_request: str,
    hypothesis: str,
    diff_text: str,
) -> tuple[str, str]:
    system = (
        "You are the independent acceptance reviewer for a coding agent. Review only the supplied request, recorded remediation hypothesis, and actual git diff. "
        "Do not continue implementation and do not assume the author model's conclusion is correct. Reject patches that merely look plausible, bypass or duplicate an existing mechanism, hard-code environment-specific values without evidence, or fail to address the causal claim. "
        "Return one JSON object only with keys accepted (boolean), reason (string), causal_alignment (boolean), existing_mechanism_checked (boolean), and acceptance_criteria_checked (boolean)."
    )
    user = (
        f"Original request:\n{original_request or '(none)'}\n\n"
        f"Current request:\n{current_request or original_request or '(none)'}\n\n"
        f"Recorded remediation hypothesis:\n{hypothesis or '(none recorded)'}\n\n"
        f"Actual git diff:\n{diff_text or '(empty diff)'}"
    )
    return system, user


def _first_json_object(text: str) -> Mapping[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_review(content: Any) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {"accepted": False, "reason": "semantic reviewer returned an empty response", "parse_error": True}
    payload: Mapping[str, Any] | None = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            payload = parsed
    except json.JSONDecodeError:
        payload = _first_json_object(text)
    if payload is None:
        return {
            "accepted": False,
            "reason": "semantic reviewer did not return parseable JSON",
            "parse_error": True,
            "raw": text[:1000],
        }
    reason = str(payload.get("reason") or "").strip()
    checks = {field: payload.get(field) is True for field in _REQUIRED_BOOLEAN_FIELDS}
    accepted = payload.get("accepted") is True and all(checks.values()) and bool(reason)
    return {
        "accepted": accepted,
        "reason": reason or "semantic reviewer omitted its reason",
        **checks,
        "parse_error": False,
    }
