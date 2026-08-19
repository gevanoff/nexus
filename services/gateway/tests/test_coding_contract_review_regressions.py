from __future__ import annotations

from app import coding_contract_hardening as hardening
from app import coding_contract_path_safety as path_safety


path_safety.install(hardening)


def test_full_path_hypothesis_does_not_add_same_basename_escape_hatch():
    target = "services/gateway/app/config.py"

    targets = hardening._resolve_asserted_targets(
        f"{target} contains the failing configuration gate",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == [target]
    assert hardening._read_matches_target(target, targets[0])
    assert not hardening._read_matches_target("other/config.py", targets[0])


def test_corrective_target_classifier_matches_repository_config_files():
    evidence = (
        "services/gateway/Dockerfile defines the runtime image; "
        "compose/nginx/gateway.conf defines proxy behavior; "
        "services/gateway/migrations/schema.sql defines storage behavior"
    )

    targets = hardening._resolve_asserted_targets(
        evidence,
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert "services/gateway/Dockerfile" in targets
    assert "compose/nginx/gateway.conf" in targets
    assert "services/gateway/migrations/schema.sql" in targets


def test_acceptance_and_context_paths_still_cannot_be_corrective_causal_targets():
    targets = hardening._resolve_asserted_targets(
        (
            "services/gateway/tests/test_config.py demonstrates the failure; "
            "docs/configuration.md documents the expected behavior"
        ),
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == []


def test_absolute_and_url_paths_are_not_reinterpreted_as_repository_paths():
    targets = hardening._resolve_asserted_targets(
        "/etc/gateway/config.py and https://example.test/services/gateway/config.py are external",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == []


def test_plain_filename_still_opens_the_intended_corrective_path():
    targets = hardening._resolve_asserted_targets(
        "config.py contains the failing configuration gate",
        {
            "causal_evidence_targets": [],
            "candidate_causal_evidence_targets": [],
        },
    )

    assert targets == ["config.py"]


def test_invalid_tool_diagnostic_is_appended_when_backend_also_returns_text():
    notice = (
        "Nexus suppressed backend tool call 'coding_apply_patch': malformed tool name. "
        "Currently authorized Coding Workspace tools: coding_update_plan."
    )

    content = hardening._content_with_diagnostic(
        "I will apply the fix now.",
        notice,
    )

    assert content.startswith("I will apply the fix now.")
    assert notice in content


def test_generic_invalid_tool_notice_is_replaced_not_duplicated():
    notice = (
        "Nexus suppressed backend tool call 'bad': malformed tool name. "
        "Currently authorized Coding Workspace tools: coding_finish."
    )

    content = hardening._content_with_diagnostic(
        hardening._INVALID_TOOL_NOTICE,
        notice,
    )

    assert content == notice
