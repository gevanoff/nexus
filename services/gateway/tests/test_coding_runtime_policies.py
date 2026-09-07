from __future__ import annotations

from fastapi import HTTPException

from app import coding_backend_failover as failover
from app import coding_semantic_acceptance as acceptance


def test_full_generation_read_timeout_excludes_only_that_backend():
    exc = HTTPException(
        status_code=502,
        detail={"upstream": "local_mlx", "error": "ReadTimeout: read timeout after 600s"},
    )

    assert failover.is_full_generation_read_timeout(exc) is True
    assert failover.retry_exclusions_after_error(set(), backend="local_mlx", exc=exc) == {"local_mlx"}
    assert [item["backend"] for item in failover.filter_candidates(
        [{"backend": "local_mlx"}, {"backend": "local_vllm_fast"}],
        {"local_mlx"},
    )] == ["local_vllm_fast"]


def test_connect_and_short_transient_errors_do_not_poison_backend():
    connect = HTTPException(
        status_code=502,
        detail={"upstream": "local_mlx", "error": "ConnectTimeout: connect timeout after 10s per attempt"},
    )
    transient = HTTPException(
        status_code=503,
        detail={"upstream": "local_mlx", "body": "temporarily unavailable"},
    )

    assert failover.is_full_generation_read_timeout(connect) is False
    assert failover.is_full_generation_read_timeout(transient) is False
    assert failover.retry_exclusions_after_error(set(), backend="local_mlx", exc=connect) == set()


def test_semantic_acceptance_requires_all_independent_checks():
    accepted = acceptance.parse_review(
        '{"accepted":true,"reason":"Patch follows the causal mechanism.",'
        '"causal_alignment":true,"existing_mechanism_checked":true,'
        '"acceptance_criteria_checked":true}'
    )
    incomplete = acceptance.parse_review(
        '{"accepted":true,"reason":"Looks plausible.",'
        '"causal_alignment":true,"existing_mechanism_checked":false,'
        '"acceptance_criteria_checked":true}'
    )

    assert accepted["accepted"] is True
    assert incomplete["accepted"] is False


def test_semantic_acceptance_recovers_first_valid_object_from_wrapped_prose():
    review = acceptance.parse_review(
        'Reviewer preface with a stray {not-json} example.\n'
        '{"accepted":false,"reason":"Existing mechanism was not checked.",'
        '"causal_alignment":true,"existing_mechanism_checked":false,'
        '"acceptance_criteria_checked":true}\n'
        'Trailing note with another {brace}.'
    )

    assert review["parse_error"] is False
    assert review["accepted"] is False
    assert review["existing_mechanism_checked"] is False
    assert review["reason"] == "Existing mechanism was not checked."


def test_semantic_acceptance_prompt_is_author_independent_and_diff_grounded():
    system, user = acceptance.build_review_messages(
        original_request="Restore the management link.",
        current_request="Fix it.",
        hypothesis="Root cause: configured URL is not rendered.",
        diff_text="+ hard-coded localhost link",
    )

    assert "independent acceptance reviewer" in system
    assert "do not assume the author model's conclusion is correct" in system.lower()
    assert "bypass or duplicate an existing mechanism" in system
    assert "hard-code environment-specific values" in system
    assert "trace the concrete control and data flow" in system
    assert "do not invent a hypothetical later overwrite" in system
    assert "reject for evidence insufficiency" in system
    assert "omitted scope or acceptance-relevant effect" in system
    assert "Keep the verdict logically consistent with the reason" in system
    assert "Actual git diff" in user
    assert "+ hard-coded localhost link" in user


def test_semantic_acceptance_prompt_requires_concrete_failure_in_final_state():
    system, user = acceptance.build_review_messages(
        original_request="Keep management metadata when model discovery fails.",
        current_request="Finish the mission.",
        hypothesis="Remove the early return from the RuntimeError branch.",
        diff_text=(
            "except RuntimeError as exc:\n"
            "    entry['models_error'] = str(exc)\n"
            "-   return entry\n"
            "entry['model_management'] = {'ui_url': ui_url}\n"
            "return entry"
        ),
    )

    assert "final program state" in system
    assert "When the supplied evidence is complete" in system
    assert "exact present or missing statement, branch, or effect" in system
    assert "hypothetical later overwrite" in system
    assert "entry['models_error'] = str(exc)" in user
    assert "entry['model_management']" in user
