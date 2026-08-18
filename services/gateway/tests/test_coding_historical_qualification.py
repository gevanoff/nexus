from __future__ import annotations

import json
from pathlib import Path

from app import coding_agent
from app import coding_debug_report
from app import coding_forced_action as forced_action
from app import coding_semantic_acceptance
from app import coding_stagnation_resilience as resilience


GATEWAY_ROOT = Path(__file__).resolve().parents[1]


def test_coder_alias_is_already_configured_for_large_repo_context() -> None:
    payload = json.loads((GATEWAY_ROOT / "app" / "model_aliases.json").read_text(encoding="utf-8"))
    coder = payload["aliases"]["coder"]

    assert coder["backend"] == "local_mlx"
    assert coder["model"] == "mlx-community/GLM-5.2-4bit"
    assert coder["context_window"] == 131_072
    assert coder["max_input_tokens"] == 100_000
    assert coder["coding_context_reset_tokens"] == 90_000
    assert coder["max_tokens_cap"] == 16_384
    assert coder["thinking_enabled"] is True


def test_debug_report_makes_effective_context_and_evidence_gate_visible() -> None:
    report = coding_debug_report.render_debug_report(
        {
            "schema": "nexus_coding_debug_report.v1",
            "generated_at": "2026-08-14T00:00:00+00:00",
            "sharing_notice": "test",
            "workspace": {
                "id": "code_example",
                "status": "ready",
                "repo_url": "https://github.com/example/repo.git",
                "base_branch": "main",
                "branch_name": "nexus-coder/code_example",
                "coding_model": "coder",
                "run_start_head": "abc123",
            },
            "agent": {
                "status": "running",
                "run_id": "run-example",
                "cycle": 7,
                "backend": "local_mlx",
                "upstream_model": "mlx-community/GLM-5.2-4bit",
            },
            "controller": {
                "progress_state": {"stagnant_cycles": 5},
                "forced_action": {
                    "action_kind": "evidence",
                    "canonical_action_kind": "edit",
                    "allowed_tools": [
                        "coding_search_text",
                        "coding_read_file_lines",
                        "coding_update_plan",
                        "coding_finish",
                    ],
                    "targeted_evidence_count": 1,
                    "hypothesis_ready": False,
                },
                "investigation_checkpoint": {"cycle": 7},
            },
            "model_runtime": {
                "resolved_alias": "coder",
                "context_window": 131_072,
                "max_input_tokens": 100_000,
                "max_tokens_cap": 16_384,
                "effective_context_reset_tokens": 90_000,
            },
            "git": {"changes": {"counts": {"total": 0}, "files": []}},
            "recent_events": [],
        }
    )

    assert "Context window / max input / output cap: `131072` / `100000` / `16384`" in report
    assert "Effective context reset tokens: `90000`" in report
    assert "Forced action: `evidence` / canonical `edit`" in report
    assert "Evidence / hypothesis: `1` / `no`" in report
    assert "coding_update_plan" in report


def test_evidence_mode_prompt_explicitly_overrides_legacy_forced_prohibition() -> None:
    task = {
        "id": "code_qualification_prompt",
        "prompt": "Fix the image management-link regression.",
        "agent_run_id": "run-qualification",
        "agent_cycle": 6,
        "base_branch": "main",
        "branch_name": "qualification",
        "project_plan": {"revision": 0, "goal": "repair", "items": [], "note": ""},
        "agent_progress_state": {
            "stagnant_cycles": 6,
            "observation": {
                "workspace_fingerprint": "same",
                "validation_revision": 0,
                "diff_review_revision": 0,
                "finish_state": "running",
            },
        },
    }
    task["agent_forced_action"] = forced_action.activate(
        task,
        state_key=resilience.durable_state_key(task),
        run_id="run-qualification",
        cycle=6,
        stage="interrupt",
        required_action="Take one bounded execution action, or finish with a concrete blocker.",
        action_kind="bounded",
    )

    prompt = coding_agent._system_prompt(task)

    legacy = "Do not inspect, orient, revise the project plan, or run arbitrary shell commands."
    override = "The execution policy applies an explicit causal-evidence provenance gate."
    assert legacy in prompt
    assert override in prompt
    assert prompt.index(override) > prompt.index(legacy)
    assert "coding_search_text" in prompt
    assert "coding_read_file_lines" in prompt
    assert "editing is not yet authorized" in prompt


def test_historical_pr71_case_is_inside_semantic_acceptance_contract() -> None:
    system, user = coding_semantic_acceptance.build_review_messages(
        original_request=(
            "The Image UI seems to have lost the link to the InvokeAI backend interface. "
            "Determine whether it was removed and restore it if necessary."
        ),
        current_request="Restore the InvokeAI model-management navigation correctly.",
        hypothesis=(
            "Root cause: the link is missing from image.html.\n"
            "Repository evidence: image.html has no fixed InvokeAI anchor.\n"
            "Competing explanation checked: none recorded.\n"
            "Expected result: a visible InvokeAI link appears."
        ),
        diff_text=(
            "+<div id=\"invokeai-backend-link\">\n"
            "+  <a href=\"http://localhost:9090\">InvokeAI Backend</a>\n"
            "+</div>"
        ),
    )

    assert "bypass or duplicate an existing mechanism" in system
    assert "hard-code environment-specific values without evidence" in system
    assert "Actual git diff" in user
    assert "http://localhost:9090" in user

    review = coding_semantic_acceptance.parse_review(
        json.dumps(
            {
                "accepted": False,
                "reason": (
                    "The patch hard-codes browser localhost and does not establish whether "
                    "the existing model_management.ui_url mechanism already supplies the link."
                ),
                "causal_alignment": False,
                "existing_mechanism_checked": False,
                "acceptance_criteria_checked": True,
            }
        )
    )
    assert review["accepted"] is False
    assert review["existing_mechanism_checked"] is False


def test_guarded_entrypoint_installs_failover_and_acceptance_hooks() -> None:
    from app import coding_agent_guarded as guarded

    assert guarded._agent._call_backend_chat_with_retry is guarded._call_backend_chat_with_failover
    assert guarded._agent._run_tool is guarded._run_tool_with_semantic_acceptance
