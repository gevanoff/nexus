from __future__ import annotations

from app import coding_edit_evidence_continuity as continuity


TARGET = "services/gateway/app/ui_routes.py"


class Persistence:
    @staticmethod
    def _normalized_path(value):
        return str(value or "").strip().replace("\\", "/").strip("/")

    @staticmethod
    def _verified_targets(state):
        return [str(item) for item in (state.get("causal_evidence_targets") or [])]

    @staticmethod
    def _successful_event_result(event):
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False or result.get("error"):
            return {}
        return result


def test_bounded_read_preserves_indentation_and_trailing_whitespace():
    source = "    indented = True  \n        nested = 1    \n"
    task = {
        "agent_events": [
            {
                "type": "tool_finished",
                "name": "coding_read_file_lines",
                "result": {
                    "ok": True,
                    "path": TARGET,
                    "content": source,
                },
            }
        ]
    }
    state = {
        "causal_evidence_targets": [TARGET],
        "hypothesis_causal_targets": [TARGET],
    }

    digest, metadata = continuity.verified_evidence_bundle(
        Persistence(),
        task,
        state,
    )

    assert digest == f"Repository path: {TARGET}\n{source}"
    assert metadata == [
        {
            "path": TARGET,
            "source_chars": len(source),
            "replayed_chars": len(source),
            "clipped": False,
        }
    ]
