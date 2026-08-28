from __future__ import annotations

import contextvars
import json
from typing import Any, Mapping


_REQUIRED_BOOLEAN_FIELDS = (
    "causal_alignment",
    "existing_mechanism_checked",
    "acceptance_criteria_checked",
)
_REVIEW_GROUNDING = contextvars.ContextVar(
    "nexus_coding_semantic_review_grounding",
    default={},
)


def set_review_grounding(*, acceptance_contract: str = "", repository_evidence: str = ""):
    """Attach immutable intent and repository-grounded context to one async review."""
    return _REVIEW_GROUNDING.set(
        {
            "acceptance_contract": str(acceptance_contract or "").strip(),
            "repository_evidence": str(repository_evidence or "").strip(),
        }
    )


def reset_review_grounding(token: Any) -> None:
    _REVIEW_GROUNDING.reset(token)


def build_review_messages(
    *,
    original_request: str,
    current_request: str,
    hypothesis: str,
    diff_text: str,
) -> tuple[str, str]:
    grounding = _REVIEW_GROUNDING.get()
    grounding = grounding if isinstance(grounding, Mapping) else {}
    acceptance_contract = str(grounding.get("acceptance_contract") or "").strip()
    repository_evidence = str(grounding.get("repository_evidence") or "").strip()

    system = (
        "You are the independent acceptance reviewer for a coding agent. Review only the supplied immutable mission intent, requests, repository evidence, recorded remediation hypothesis, and actual git diff. "
        "The remediation hypothesis and project-plan narrative are author-controlled claims, not acceptance criteria or ground truth. Never accept a patch merely because it matches that hypothesis. "
        "Treat the immutable acceptance contract as authoritative human/controller intent and treat verified repository evidence and the actual diff as ground truth. "
        "Reject patches that merely look plausible, bypass or duplicate an existing mechanism, hard-code environment-specific values without evidence, substitute one address/identity/transport for another without repository evidence that they are equivalent, fix only a success path when the requested behavior must survive a relevant failure path, or fail to address the causal claim. "
        "When an explicit acceptance criterion is supplied, acceptance_criteria_checked may be true only if the diff plus repository evidence demonstrate that criterion rather than merely asserting it. "
        "Return one JSON object only with keys accepted (boolean), reason (string), causal_alignment (boolean), existing_mechanism_checked (boolean), and acceptance_criteria_checked (boolean)."
    )
    user = (
        f"Immutable acceptance contract:\n{acceptance_contract or '(original request only; no additional criteria supplied)'}\n\n"
        f"Original request:\n{original_request or '(none)'}\n\n"
        f"Current request:\n{current_request or original_request or '(none)'}\n\n"
        f"Verified repository context:\n{repository_evidence or '(no additional repository context available)'}\n\n"
        f"Recorded remediation hypothesis (untrusted author claim):\n{hypothesis or '(none recorded)'}\n\n"
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
