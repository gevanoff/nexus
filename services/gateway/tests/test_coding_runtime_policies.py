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
    assert "Actual git diff" in user
    assert "+ hard-coded localhost link" in user
