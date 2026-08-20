from __future__ import annotations

from types import SimpleNamespace

from app import coding_evidence_policy as provenance
from app import coding_evidence_range_provenance as range_provenance
from app import coding_failed_edit_recovery as recovery


TARGET = "services/gateway/app/ui_routes.py"
FIXTURE = "services/gateway/tests/test_image_ui_enhancements.py"


def _modified_workspace(name: str, args: dict, result: dict) -> bool:
    if not bool(result.get("ok")):
        return False
    if name == "coding_replace_text":
        return int(result.get("replacements") or 0) > 0
    if name == "coding_apply_patch":
        return not bool(args.get("check_only") or result.get("check_only"))
    return name == "coding_write_file"


def _plan() -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_update_plan",
        "ts": 10.0,
        "result": {"ok": True, "plan": {"revision": 2}},
    }


def _replace_events(*, ok: bool, replacements: int, ts: float, cycle: int) -> list[dict]:
    call_id = f"replace-{cycle}"
    result = {
        "ok": ok,
        "path": TARGET,
        "replacements": replacements,
    }
    if not ok:
        result["error"] = "old_text was not found"
    return [
        {
            "type": "tool_started",
            "name": "coding_replace_text",
            "tool_call_id": call_id,
            "cycle": cycle,
            "ts": ts,
            "args": {
                "path": TARGET,
                "old_text": "old",
                "new_text": "new",
            },
        },
        {
            "type": "tool_finished",
            "name": "coding_replace_text",
            "tool_call_id": call_id,
            "cycle": cycle,
            "ts": ts,
            "result": result,
        },
    ]


def _edit_state() -> dict:
    return {
        "action_kind": "edit",
        "allowed_tools": [
            "coding_apply_patch",
            "coding_finish",
            "coding_replace_text",
            "coding_write_file",
        ],
        "durable_hypothesis_note_updated_at": 10.0,
    }


def test_later_successful_mutation_supersedes_earlier_failed_exact_edit():
    task = {
        "agent_events": [
            _plan(),
            *_replace_events(ok=False, replacements=0, ts=20.0, cycle=3),
            *_replace_events(ok=True, replacements=1, ts=30.0, cycle=4),
        ]
    }
    agent = SimpleNamespace(_tool_result_modified_workspace=_modified_workspace)

    state = recovery.refine_state(agent, task, _edit_state())

    assert state["action_kind"] == "edit"
    assert "failed_edit_refresh_required" not in state


def _read(path: str, *, start: int, end: int, ts: float) -> dict:
    return {
        "type": "tool_finished",
        "name": "coding_read_file_lines",
        "ts": ts,
        "result": {
            "ok": True,
            "path": path,
            "start_line": start,
            "end_line": end,
            "line_count": end - start + 1,
            "content": "verified source",
        },
    }


def test_range_metadata_exposes_only_provenance_classified_causal_targets():
    state = {
        "causal_evidence_targets": [TARGET],
        "acceptance_evidence_targets": [FIXTURE],
    }
    task = {
        "agent_events": [
            _read(TARGET, start=1600, end=1650, ts=10.0),
            _read(FIXTURE, start=1, end=80, ts=11.0),
        ]
    }

    ranges, legacy = range_provenance._verified_ranges(
        provenance,
        object(),
        task,
        state,
    )

    assert legacy == set()
    assert ranges == {TARGET: [(1600, 1650)]}
    assert FIXTURE not in ranges


def test_successful_range_match_clears_stale_range_requirement_flags():
    state = {
        "action_kind": "edit",
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
        "hypothesis_causal_evidence_linked": True,
        "hypothesis_evidence_range_required": True,
        "hypothesis_evidence_range_targets": [TARGET],
    }
    task = {
        "agent_events": [_read(TARGET, start=1600, end=1650, ts=10.0)],
        "project_plan": {
            "revision": 2,
            "note": (
                "Root cause: catalog failure skips management metadata.\n"
                f"Repository evidence: {TARGET}:1600-1650 shows the early return and management attachment.\n"
                "Competing explanation checked: frontend rendering still consumes management.ui_url.\n"
                "Expected result: management URL survives model catalog failure."
            ),
        },
    }

    class Base:
        _HYPOTHESIS_FIELDS = (
            "Root cause",
            "Repository evidence",
            "Competing explanation checked",
            "Expected result",
        )
        _HYPOTHESIS_FIELD_RE = __import__("re").compile(
            r"(?im)(?:^|[.;]\s*)(Root cause|Repository evidence|Competing explanation checked|Expected result)\s*:\s*"
        )

        @classmethod
        def _structured_hypothesis(cls, task, state):
            note = task["project_plan"]["note"]
            matches = list(cls._HYPOTHESIS_FIELD_RE.finditer(note))
            fields = {}
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(note)
                fields[match.group(1)] = note[match.end():end].strip()
            return len(fields) == 4, fields

    refined = range_provenance.refine_state(provenance, Base, task, state)

    assert refined["action_kind"] == "edit"
    assert refined["hypothesis_causal_evidence_linked"] is True
    assert "hypothesis_evidence_range_required" not in refined
    assert "hypothesis_evidence_range_targets" not in refined
