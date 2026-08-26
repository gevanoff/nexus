from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import coding_hypothesis_persistence as hypothesis_persistence
from app import coding_hypothesis_transition_hardening as transition_hardening
from app import coding_policy_evidence_continuity_hardening as hardening


PATH = "services/gateway/app/static/image_catalog_ui.js"


class ToolFunction:
    def __init__(self, *, name: str, description: str = "", parameters=None):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}


class ToolSpec:
    def __init__(self, *, function):
        self.function = function


def spec(name: str) -> ToolSpec:
    return ToolSpec(function=ToolFunction(name=name))


class Persistence:
    @staticmethod
    def _normalized_path(value):
        return str(value or "").strip().replace("\\", "/").strip("/")

    @staticmethod
    def _verified_targets(state):
        return list(state.get("causal_evidence_targets") or [])

    @staticmethod
    def _successful_event_result(event):
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False or result.get("error"):
            return {}
        return result


class Policy:
    def __init__(self, state: dict):
        self.state = dict(state)

    def active_state(self, _task):
        return dict(self.state)

    def allowed_tool_names(self, task):
        return set(self.active_state(task).get("allowed_tools") or [])

    def filter_tool_specs(self, specs, task):
        allowed = self.allowed_tool_names(task)
        return [item for item in specs if item.function.name in allowed]

    def evaluate_tool_call(self, task, *, name, args, is_validation_command):
        del args, is_validation_command
        allowed = name in self.allowed_tool_names(task)
        if allowed:
            return True, {}
        return False, {
            "ok": False,
            "error": "forced_action_tool_rejected",
            "allowed_tools": sorted(self.allowed_tool_names(task)),
            "required_action": self.active_state(task).get("required_action"),
        }

    def prompt_context(self, _task):
        return "STALE OR BASE PROMPT"


class CW:
    def __init__(self, task):
        self.task = task

    def load_task(self, _task_id):
        return self.task

    def coding_state_snapshot(self, _task_id):
        return {
            "changes": {"counts": {"total": 0}, "changed_files": []},
            "progress": {
                "current_phase": "editing",
                "next_recommended_action": "stale guidance",
            },
        }


class Debug:
    @staticmethod
    def redact_text(value, *, limit=4000):
        return str(value or "")[:limit]

    @staticmethod
    def _sanitize(value, **_kwargs):
        return value

    @staticmethod
    def _event_view(event):
        return {"type": event.get("type"), "name": event.get("name")}

    @staticmethod
    def _durable_state_view(result):
        return {"ok": bool(result.get("ok"))}


class EvidencePolicy:
    @staticmethod
    def _repository_evidence_links_target(repository_evidence, target):
        return str(target) in str(repository_evidence)


def test_multi_range_replay_preserves_distinct_verified_ranges_from_same_file():
    state = {
        "action_kind": "edit",
        "causal_evidence_targets": [PATH],
        "hypothesis_causal_targets": [PATH],
        "causal_evidence_ranges": [
            {"path": PATH, "start_line": 60, "end_line": 89},
            {"path": PATH, "start_line": 150, "end_line": 269},
        ],
    }
    task = {
        "agent_events": [
            {
                "type": "tool_finished",
                "name": "coding_read_file_lines",
                "result": {
                    "ok": True,
                    "path": PATH,
                    "start_line": 60,
                    "end_line": 89,
                    "content": "function safeExternalUrl(value) { return value; }",
                },
            },
            {
                "type": "tool_finished",
                "name": "coding_read_file_lines",
                "result": {
                    "ok": True,
                    "path": PATH,
                    "start_line": 150,
                    "end_line": 269,
                    "content": "function renderBackendSummary(entry) { const uiUrl = safeExternalUrl(entry.model_management.ui_url); }",
                },
            },
        ]
    }

    digest, metadata = hardening.multi_range_verified_evidence_bundle(
        Persistence(),
        task,
        state,
    )

    assert f"{PATH}:60-89" in digest
    assert f"{PATH}:150-269" in digest
    assert "safeExternalUrl" in digest
    assert "renderBackendSummary" in digest
    assert "model_management.ui_url" in digest
    assert [(item["start_line"], item["end_line"]) for item in metadata] == [
        (60, 89),
        (150, 269),
    ]


def test_transition_overlay_uses_live_agent_policy_not_stale_base_module():
    task = {
        "id": "code-live-policy",
        "project_plan": {
            "revision": 1,
            "items": [],
            "note": "durable hypothesis already persisted",
        },
    }
    live = Policy(
        {
            "action_kind": "edit",
            "evidence_provenance_enforced": True,
            "hypothesis_causal_evidence_linked": True,
            "durable_hypothesis_note_ready": True,
            "allowed_tools": ["coding_finish", "coding_write_file"],
            "required_action": "Make the smallest evidence-backed edit, or finish with a concrete blocker.",
        }
    )
    stale = Policy(
        {
            "action_kind": "evidence",
            "durable_hypothesis_note_ready": False,
            "allowed_tools": ["coding_finish", "coding_update_plan"],
            "required_action": "Persist the four-field hypothesis before editing.",
        }
    )
    raw_specs = [
        spec("coding_finish"),
        spec("coding_update_plan"),
        spec("coding_write_file"),
        spec("coding_refute_hypothesis"),
    ]
    agent = SimpleNamespace(
        ToolFunction=ToolFunction,
        ToolSpec=ToolSpec,
        forced_action=live,
        _tool_specs=lambda: list(raw_specs),
        _tool_specs_for_task=lambda _task: list(raw_specs),
    )
    cw = CW(task)

    transition_hardening.install(
        agent,
        cw,
        stale,
        hypothesis_persistence,
        EvidencePolicy(),
        Debug(),
    )

    names = {item.function.name for item in agent._tool_specs_for_task(task)}
    assert names == {"coding_finish", "coding_write_file", "coding_refute_hypothesis"}
    allowed, rejection = live.evaluate_tool_call(
        task,
        name="coding_write_file",
        args={"path": PATH, "content": "x"},
        is_validation_command=lambda _argv: False,
    )
    assert allowed is True
    assert rejection == {}
    progress = cw.coding_state_snapshot("code-live-policy")["progress"]
    assert progress["next_recommended_action"].startswith("Make the smallest evidence-backed edit")
    prompt = live.prompt_context(task)
    assert "effective controller state for this turn is edit" in prompt
    assert "coding_refute_hypothesis" in prompt


def test_dispatch_policy_invariant_rejects_edit_state_with_evidence_schema():
    edit_state = {
        "action_kind": "edit",
        "allowed_tools": ["coding_finish", "coding_write_file"],
    }
    agent = SimpleNamespace(forced_action=Policy(edit_state))
    dispatch = SimpleNamespace(
        coding_execution_policy=SimpleNamespace(execution_task=lambda _agent, task: dict(task)),
        _request_value=lambda req, name, default=None: req.get(name, default),
    )
    snapshot = SimpleNamespace(
        allowed_tools=("coding_finish", "coding_update_plan"),
        text_tool_mode=False,
    )
    materialized = {
        "tools": [spec("coding_finish"), spec("coding_update_plan")],
    }

    with pytest.raises(RuntimeError, match="coding_execution_policy_contract_mismatch"):
        hardening.assert_execution_policy_consistency(
            agent,
            dispatch,
            {},
            materialized,
            snapshot,
            {"coding_request": True},
        )


def test_dispatch_policy_invariant_accepts_exact_effective_schema():
    edit_state = {
        "action_kind": "edit",
        "allowed_tools": ["coding_finish", "coding_write_file"],
    }
    agent = SimpleNamespace(forced_action=Policy(edit_state))
    dispatch = SimpleNamespace(
        coding_execution_policy=SimpleNamespace(execution_task=lambda _agent, task: dict(task)),
        _request_value=lambda req, name, default=None: req.get(name, default),
    )
    snapshot = SimpleNamespace(
        allowed_tools=("coding_finish", "coding_write_file"),
        text_tool_mode=False,
    )
    materialized = {
        "tools": [spec("coding_finish"), spec("coding_write_file")],
    }

    hardening.assert_execution_policy_consistency(
        agent,
        dispatch,
        {},
        materialized,
        snapshot,
        {"coding_request": True},
    )
