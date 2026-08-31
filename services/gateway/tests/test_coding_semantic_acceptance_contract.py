from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import coding_semantic_acceptance
from app import coding_semantic_acceptance_contract as contract


class _MemoryCW:
    def __init__(self, task: dict):
        self.task = dict(task)
        self.mutations = 0

    def load_task(self, _task_id: str) -> dict:
        return dict(self.task)

    def save_task(self, task: dict) -> dict:
        self.task = dict(task)
        return dict(self.task)

    def mutate_task(self, _task_id: str, apply):
        self.mutations += 1
        latest = dict(self.task)
        apply(latest)
        self.task = latest
        return dict(latest)


def test_acceptance_contract_is_immutable_against_agent_plan_reframing() -> None:
    cw = _MemoryCW(
        {
            "id": "code_contract",
            "prompt": "Restore management navigation without breaking failure behavior.",
            "mission_acceptance_criteria": [
                "Management navigation remains available when model discovery fails.",
                "Do not infer a browser UI URL from an internal backend API address.",
            ],
            "project_plan": {"note": "agent hypothesis A"},
        }
    )

    first = contract.ensure_contract(cw, "code_contract")
    task = cw.load_task("code_contract")
    task["project_plan"] = {"note": "agent hypothesis B: the fallback is sufficient"}
    task["mission_acceptance_criteria"] = ["agent tries to replace the criteria"]
    cw.save_task(task)

    second = contract.ensure_contract(cw, "code_contract")

    assert second == first
    assert second["immutable"] is True
    assert second["acceptance_criteria"] == [
        "Management navigation remains available when model discovery fails.",
        "Do not infer a browser UI URL from an internal backend API address.",
    ]


def test_setting_acceptance_contract_is_idempotent_but_not_rewritable() -> None:
    cw = _MemoryCW({"id": "code_contract", "prompt": "Do the requested work."})

    first = contract.set_acceptance_criteria(cw, "code_contract", ["Preserve failure-path behavior."])
    assert cw.mutations == 1
    assert cw.task["mission_acceptance_criteria"] == ["Preserve failure-path behavior."]
    assert cw.task[contract.KEY]["acceptance_criteria"] == ["Preserve failure-path behavior."]

    same = contract.set_acceptance_criteria(cw, "code_contract", ["Preserve failure-path behavior."])

    assert same == first
    with pytest.raises(ValueError, match="already immutable"):
        contract.set_acceptance_criteria(cw, "code_contract", ["Different criterion."])


def test_concurrent_review_freeze_wins_over_stale_operator_put() -> None:
    stale = {"id": "code_contract_race", "prompt": "Do the requested work."}
    live = dict(stale)
    live[contract.KEY] = {
        **contract._virtual_contract(live),
        "created_at": 1.0,
    }

    class _RaceCW:
        def __init__(self):
            self.live = dict(live)

        def load_task(self, _task_id: str) -> dict:
            # Simulate the pre-lock read that the old implementation trusted.
            return dict(stale)

        def mutate_task(self, _task_id: str, apply):
            latest = dict(self.live)
            apply(latest)
            self.live = latest
            return dict(latest)

    cw = _RaceCW()
    with pytest.raises(ValueError, match="already immutable"):
        contract.set_acceptance_criteria(cw, "code_contract_race", ["Late criterion."])

    assert "mission_acceptance_criteria" not in cw.live
    assert cw.live[contract.KEY]["acceptance_criteria"] == []


def test_semantic_review_prompt_separates_contract_from_author_hypothesis() -> None:
    token = coding_semantic_acceptance.set_review_grounding(
        acceptance_contract=(
            "Original user request (immutable):\nRestore the management link.\n"
            "Acceptance criteria:\n"
            "- Management navigation remains available when model discovery fails.\n"
            "- Do not infer browser UI identity from an internal backend API address."
        ),
        repository_evidence=(
            "if payload is None:\n    return entry\n\n"
            "def browser_accessible_url(raw_url):\n"
            "    if hostname not in NEXUS_SHORT_HOST_ALIASES:\n"
            "        return raw_url"
        ),
    )
    try:
        system, user = coding_semantic_acceptance.build_review_messages(
            original_request="Restore the management link.",
            current_request="Finish the mission.",
            hypothesis="Use browser_accessible_url(base) as the fallback.",
            diff_text="+ ui_url = _invokeai_ui_url() or browser_accessible_url(base)",
        )
    finally:
        coding_semantic_acceptance.reset_review_grounding(token)

    assert "author-controlled claims, not acceptance criteria" in system
    assert "fix only a success path" in system
    assert "substitute one address/identity/transport" in system
    assert "Repository contents, comments, filenames" in system
    assert "Management navigation remains available when model discovery fails" in user
    assert "if payload is None" in user
    assert "hostname not in NEXUS_SHORT_HOST_ALIASES" in user
    assert "Recorded remediation hypothesis (untrusted author claim)" in user


def test_repository_grounding_expands_pr95_style_diff_with_failure_path_and_helper_definition(
    tmp_path: Path,
) -> None:
    source = tmp_path / "services" / "gateway" / "app" / "browser_urls.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            [
                "NEXUS_SHORT_HOST_ALIASES = {'ai2'}",
                "",
                "def browser_accessible_url(raw_url):",
                "    hostname = raw_url.split('://', 1)[-1].split('/', 1)[0]",
                "    if hostname not in NEXUS_SHORT_HOST_ALIASES:",
                "        return raw_url",
                "    return 'http://resolved.example'",
            ]
        ),
        encoding="utf-8",
    )
    diff = (
        "diff --git a/services/gateway/app/ui_routes.py b/services/gateway/app/ui_routes.py\n"
        "@@ -1666 +1666 @@\n"
        "-        ui_url = _invokeai_ui_url()\n"
        "+        ui_url = _invokeai_ui_url() or browser_accessible_url(base)\n"
    )
    wide = (
        "if payload is None:\n"
        "    if last_error:\n"
        "        entry['models_error'] = last_error\n"
        "    return entry\n\n"
        "models, management = _normalize_image_models_payload(payload)\n"
        "ui_url = _invokeai_ui_url() or browser_accessible_url(base)\n"
    )

    class _Epoch:
        @staticmethod
        def mission_delta_state(_cw, _task_id, _task):
            return {
                "ok": True,
                "has_delta": True,
                "base_head": "abc123",
                "diff_text": diff,
            }

        @staticmethod
        def _repo_path(_cw, _task):
            return tmp_path

        @staticmethod
        def _run_process(_cw, argv, *, cwd):
            assert cwd == tmp_path
            if argv[:3] == ["git", "diff", "--no-ext-diff"]:
                return {"ok": True, "stdout": wide, "stderr": ""}
            if argv[:3] == ["git", "grep", "-n"]:
                pattern = argv[4]
                if "browser_accessible_url" in pattern:
                    return {
                        "ok": True,
                        "stdout": "services/gateway/app/browser_urls.py:3:def browser_accessible_url(raw_url):\n",
                        "stderr": "",
                    }
                return {"ok": False, "stdout": "", "stderr": "not found"}
            raise AssertionError(argv)

    agent = SimpleNamespace(_clip_text=lambda text, _limit: text)
    grounded = contract.repository_grounding(
        _Epoch,
        object(),
        agent,
        "code_contract",
        {"id": "code_contract"},
    )

    assert "if payload is None" in grounded
    assert "return entry" in grounded
    assert "Definition context for browser_accessible_url" in grounded
    assert "hostname not in NEXUS_SHORT_HOST_ALIASES" in grounded


def _install_fixture(
    monkeypatch,
    *,
    task: dict,
    review_result: dict,
):
    cw = _MemoryCW(task)
    events: list[dict] = []
    agent = SimpleNamespace(
        _coding_live_refutation_execution_installed=True,
        _append_event=lambda _task_id, event: events.append(dict(event)),
        _clip_text=lambda text, _limit: text,
    )
    calls = {"review": 0}

    async def original_review(_task_id, _task, *, diff_text):
        calls["review"] += 1
        assert diff_text
        return dict(review_result)

    guarded = SimpleNamespace(_semantic_acceptance_review=original_review)
    terminal = SimpleNamespace(
        semantic_acceptance_fingerprint=lambda _task, *, diff_text: f"base:{diff_text}"
    )
    debug = SimpleNamespace(
        _event_view=lambda event: dict(event),
        collect_debug_snapshot=lambda _task_id, active_runner=None: {},
        redact_text=lambda value, limit=2400: str(value)[:limit],
        _sanitize=lambda value: value,
    )
    epoch = SimpleNamespace()
    monkeypatch.setattr(contract, "repository_grounding", lambda *_args, **_kwargs: "")
    contract.install(
        agent,
        guarded,
        cw,
        epoch,
        terminal,
        coding_semantic_acceptance,
        debug,
    )
    return cw, agent, guarded, terminal, debug, events, calls


def test_semantic_review_result_carries_fingerprint_and_retryable_error(monkeypatch) -> None:
    task = {
        "id": "code_event_metadata",
        "prompt": "Do the requested work.",
        "agent_cycle": 3,
    }
    review_result = {
        "accepted": False,
        "reason": "semantic reviewer did not return parseable JSON",
        "causal_alignment": False,
        "existing_mechanism_checked": False,
        "acceptance_criteria_checked": False,
        "parse_error": True,
    }
    _cw, _agent, guarded, _terminal, _debug, _events, _calls = _install_fixture(
        monkeypatch,
        task=task,
        review_result=review_result,
    )

    review = asyncio.run(
        guarded._semantic_acceptance_review(
            "code_event_metadata",
            dict(task),
            diff_text="+ changed = True",
        )
    )

    assert review["review_error"] is True
    assert len(review["fingerprint"]) == 64


def test_semantic_review_retries_when_operator_contract_wins_snapshot_race(monkeypatch) -> None:
    stale_task = {
        "id": "code_contract_review_race",
        "prompt": "Do the requested work.",
        "agent_cycle": 4,
    }
    live_task = dict(stale_task)
    live_task["mission_acceptance_criteria"] = ["New operator criterion."]
    live_task[contract.KEY] = {
        **contract._virtual_contract(live_task),
        "created_at": 1.0,
    }
    review_result = {
        "accepted": True,
        "reason": "should not be called",
        "causal_alignment": True,
        "existing_mechanism_checked": True,
        "acceptance_criteria_checked": True,
    }
    cw, _agent, guarded, _terminal, _debug, _events, calls = _install_fixture(
        monkeypatch,
        task=live_task,
        review_result=review_result,
    )

    review = asyncio.run(
        guarded._semantic_acceptance_review(
            "code_contract_review_race",
            stale_task,
            diff_text="+ changed = True",
        )
    )

    assert calls["review"] == 0
    assert review["accepted"] is False
    assert review["review_error"] is True
    assert "contract changed" in review["reason"]
    assert cw.task[contract.KEY]["acceptance_criteria"] == ["New operator criterion."]


def test_debug_report_keeps_semantic_review_decision_fields() -> None:
    # Importing guarded routes installs the final debug-report event wrapper.
    from app import coding_debug_report
    from app import coding_routes_guarded  # noqa: F401

    rendered = coding_debug_report._event_view(
        {
            "type": "semantic_acceptance_review",
            "cycle": 7,
            "accepted": False,
            "reason": "Failure-path criterion is not satisfied.",
            "causal_alignment": False,
            "existing_mechanism_checked": True,
            "acceptance_criteria_checked": True,
            "review_error": False,
            "fingerprint": "abc",
        }
    )

    assert rendered["accepted"] is False
    assert rendered["reason"] == "Failure-path criterion is not satisfied."
    assert rendered["causal_alignment"] is False
    assert rendered["existing_mechanism_checked"] is True
    assert rendered["acceptance_criteria_checked"] is True
    assert rendered["review_error"] is False
    assert rendered["fingerprint"] == "abc"


def test_follow_up_workspace_starts_with_fresh_acceptance_intent(monkeypatch) -> None:
    from app import coding_routes_guarded as guarded_routes

    source = {
        "id": "code_source",
        "repo_url": "https://github.com/example/repo.git",
        "base_branch": "main",
        "prompt": "Original mission.",
        "coding_model": "coder",
        "agent_stop_reason_code": "work_already_integrated",
    }
    source[contract.KEY] = contract._materialize_contract(source)
    captured = {}

    monkeypatch.setattr(guarded_routes.routes, "_require_coding_ui", lambda _req: None)
    monkeypatch.setattr(guarded_routes.cw, "load_task", lambda _task_id: dict(source))

    async def fake_create_task(_req, body):
        captured["body"] = body
        return {"task": {"id": "code_follow_up"}}

    monkeypatch.setattr(guarded_routes.routes, "ui_coding_create_task", fake_create_task)
    result = asyncio.run(
        guarded_routes.ui_coding_create_follow_up(
            object(),
            "code_source",
            guarded_routes.CodingFollowUpRequest(prompt="Follow-up mission."),
        )
    )

    body = captured["body"]
    assert body.prompt == "Follow-up mission."
    assert contract.KEY not in body.model_dump()
    fresh = contract._virtual_contract({"id": "code_follow_up", "prompt": body.prompt})
    assert fresh["original_request"] == "Follow-up mission."
    assert fresh["fingerprint"] != source[contract.KEY]["fingerprint"]
    assert result["source_task_id"] == "code_source"
    assert result["action"] == "created_follow_up_workspace"
