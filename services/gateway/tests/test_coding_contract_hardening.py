from __future__ import annotations

from types import SimpleNamespace

from app import coding_contract_hardening as hardening
from app import coding_forced_action as forced


def _period_plan(repository_evidence: str) -> dict:
    return {
        "revision": 1,
        "goal": "Fix the Image UI",
        "note": (
            "Root cause: the management link is gated by the wrong implementation behavior. "
            f"Repository evidence: {repository_evidence}. "
            "Competing explanation checked: the frontend renderer and target element still exist. "
            "Expected result: the configured management URL remains visible when catalog discovery fails."
        ),
        "items": [],
    }


def test_period_separated_hypothesis_fields_are_structured_contract():
    task = {
        "project_plan": _period_plan(
            "services/gateway/app/ui_routes.py returns before advertising the InvokeAI UI URL"
        )
    }
    ready, fields = hardening.structured_hypothesis(
        forced,
        task,
        {"activation_plan_revision": 0},
    )

    assert ready is True
    assert set(fields) == set(forced._HYPOTHESIS_FIELDS)
    assert "services/gateway/app/ui_routes.py" in fields["Repository evidence"]


def test_hypothesis_revision_still_must_advance_after_activation():
    task = {
        "project_plan": _period_plan(
            "services/gateway/app/ui_routes.py returns before advertising the InvokeAI UI URL"
        )
    }

    ready, fields = hardening.structured_hypothesis(
        forced,
        task,
        {"activation_plan_revision": 1},
    )

    assert ready is False
    assert fields == {}


def test_repository_path_normalization_rejects_escape_and_absolute_paths():
    assert hardening._normalized_path("services/gateway/app/ui_routes.py") == "services/gateway/app/ui_routes.py"
    assert hardening._normalized_path("./services/gateway/app/ui_routes.py") == "services/gateway/app/ui_routes.py"
    assert hardening._normalized_path("../services/gateway/app/ui_routes.py") == ""
    assert hardening._normalized_path("/etc/config.py") == ""
    assert hardening._normalized_path("C:/temp/config.py") == ""
    assert hardening._normalized_path("https://example.test/config.py") == ""


def test_bare_hypothesis_filename_is_retained_only_as_corrective_read_target():
    state = {
        "causal_evidence_targets": ["services/gateway/app/static/image_catalog_ui.js"],
        "candidate_causal_evidence_targets": [],
    }

    targets = hardening._resolve_asserted_targets(
        "The image.html template is missing the management element",
        state,
    )

    assert targets == ["image.html"]


def test_unique_known_basename_resolves_to_repository_relative_candidate():
    state = {
        "causal_evidence_targets": ["services/gateway/app/static/image_catalog_ui.js"],
        "candidate_causal_evidence_targets": ["services/gateway/app/static/image.html"],
    }

    targets = hardening._resolve_asserted_targets(
        "The image.html template is missing the management element",
        state,
    )

    assert targets == ["services/gateway/app/static/image.html"]


def test_ambiguous_known_basename_does_not_guess_a_repository_path():
    state = {
        "causal_evidence_targets": [],
        "candidate_causal_evidence_targets": [
            "services/gateway/app/config.py",
            "services/images/app/config.py",
        ],
    }

    targets = hardening._resolve_asserted_targets(
        "config.py contains the bad setting",
        state,
    )

    assert targets == ["config.py"]


def test_unverified_hypothesis_target_opens_one_corrective_read_without_editing():
    class FakeBase:
        _HYPOTHESIS_FIELDS = forced._HYPOTHESIS_FIELDS

        @staticmethod
        def _structured_hypothesis(task, state):
            return True, {
                "Root cause": "the HTML target is absent",
                "Repository evidence": "image.html is missing modelManagement",
                "Competing explanation checked": "the renderer still exists",
                "Expected result": "restoring the target makes the link visible",
            }

    fake_base = FakeBase()
    policy = SimpleNamespace(_base_policy=lambda _forced: fake_base)
    state = {
        "action_kind": "evidence",
        "evidence_provenance_enforced": True,
        "causal_evidence_targets": [
            "services/gateway/app/static/image_catalog_ui.js"
        ],
        "candidate_causal_evidence_targets": [],
        "hypothesis_causal_evidence_linked": False,
        "allowed_tools": ["coding_finish", "coding_update_plan"],
    }

    refined = hardening.refine_provenance_state(
        policy,
        object(),
        {},
        state,
    )

    assert refined["action_kind"] == "evidence"
    assert refined["hypothesis_unverified_targets"] == ["image.html"]
    assert refined["allowed_tools"] == [
        "coding_finish",
        "coding_read_file_lines",
        "coding_update_plan",
    ]
    assert "coding_apply_patch" not in refined["allowed_tools"]


def test_corrective_read_target_lock_accepts_full_path_for_matching_basename_only():
    assert hardening._read_matches_target(
        "services/gateway/app/static/image.html",
        "image.html",
    )
    assert not hardening._read_matches_target(
        "services/gateway/app/static/image_catalog_ui.js",
        "image.html",
    )
    assert hardening._read_matches_target(
        "services/gateway/app/static/image.html",
        "services/gateway/app/static/image.html",
    )
    assert not hardening._read_matches_target(
        "other/image.html",
        "services/gateway/app/static/image.html",
    )


def test_provenance_prompt_names_exact_verified_targets_after_handoff():
    state = {
        "allowed_tools": ["coding_finish", "coding_update_plan"],
        "causal_evidence_targets": [
            "services/gateway/app/static/image_catalog_ui.js",
            "services/gateway/app/ui_routes.py",
        ],
        "candidate_causal_evidence_targets": [],
        "hypothesis_unverified_targets": [],
    }

    prompt = hardening.provenance_prompt_context(forced, state)

    assert "Verified causal implementation/configuration targets are:" in prompt
    assert "services/gateway/app/static/image_catalog_ui.js" in prompt
    assert "services/gateway/app/ui_routes.py" in prompt
    assert "Do not reconstruct or infer a different target from compacted model notes" in prompt
    for label in forced._HYPOTHESIS_FIELDS:
        assert f"{label}: <specific finding>" in prompt


def test_safe_invalid_tool_notice_exposes_attempt_and_authorized_tools():
    notice = hardening.diagnostic_notice(
        {
            "name": "coding_apply_patch",
            "reason": "unknown tool name",
            "allowed_tool_names": ["coding_finish", "coding_update_plan"],
        }
    )

    assert "coding_apply_patch" in notice
    assert "unknown tool name" in notice
    assert "coding_finish" in notice
    assert "coding_update_plan" in notice


def test_blocked_finish_preserves_agent_blocker_without_successful_noop_label():
    called = []

    def original(**kwargs):
        called.append(kwargs)
        return True, "original", None

    agent = SimpleNamespace(_no_change_audit=original)
    hardening._install_blocked_finish_audit(agent)

    success, summary, event = agent._no_change_audit(
        finish_called=True,
        finish_success=False,
        finish_summary="Blocked: verified evidence contradicts the proposed edit.",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc",
        end_head="abc",
        expects_workspace_edits=True,
    )

    assert success is False
    assert summary == "Blocked: verified evidence contradicts the proposed edit."
    assert event["type"] == "blocked_finish"
    assert called == []


def test_successful_no_change_finish_still_uses_original_audit():
    called = []

    def original(**kwargs):
        called.append(kwargs)
        return False, "no changes", {"type": "no_change_audit"}

    agent = SimpleNamespace(_no_change_audit=original)
    hardening._install_blocked_finish_audit(agent)

    success, summary, event = agent._no_change_audit(
        finish_called=True,
        finish_success=True,
        finish_summary="done",
        committed_changes=False,
        uncommitted_changes=False,
        start_head="abc",
        end_head="abc",
        expects_workspace_edits=True,
    )

    assert success is False
    assert summary == "no changes"
    assert event["type"] == "no_change_audit"
    assert len(called) == 1


def test_debug_event_view_surfaces_existing_execution_policy_fields():
    debug = SimpleNamespace(
        _event_view=lambda event: {"type": event["type"], "summary": event.get("summary", "")},
        _sanitize=lambda value, **kwargs: value,
    )
    hardening._install_debug_event_view(debug)

    view = debug._event_view(
        {
            "type": "execution_policy_transition",
            "summary": "rematerialized",
            "action_kind": "edit",
            "allowed_tools": ["coding_apply_patch", "coding_finish"],
            "causal_evidence_targets": ["services/gateway/app/ui_routes.py"],
            "text_tool_mode": True,
            "converted_tool_results": 3,
        }
    )

    assert view["action_kind"] == "edit"
    assert view["allowed_tools"] == ["coding_apply_patch", "coding_finish"]
    assert view["causal_evidence_targets"] == ["services/gateway/app/ui_routes.py"]
    assert view["text_tool_mode"] is True
    assert view["converted_tool_results"] == 3
