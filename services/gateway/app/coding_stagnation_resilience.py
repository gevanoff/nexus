from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SCHEMA = "nexus_coding_stagnation_controller.v2"
WORKING_MEMORY_SCHEMA = "nexus_coding_working_memory.v2"
CONTEXT_MANIFEST_SCHEMA = "nexus_coding_context_manifest.v1"

_INSPECTION_TOOLS = {
    "coding_search_text",
    "coding_read_file",
    "coding_read_file_lines",
    "coding_list_tree",
    "coding_git_status",
    "coding_git_diff",
    "coding_change_summary",
}
_VALIDATION_TOOLS = {"coding_run_command"}
_EDIT_TOOLS = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}
_REVIEW_TOOLS = {"coding_git_status", "coding_git_diff", "coding_change_summary"}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "what", "where", "with",
}


@dataclass(frozen=True)
class StageThresholds:
    assist: int
    interrupt: int
    terminal: int


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def event_fingerprint(event: Mapping[str, Any]) -> str:
    """Return an opaque cursor for one persisted event.

    The cursor deliberately hashes the complete event rather than persisting
    command arguments or assistant content in controller metadata.
    """
    return stable_hash(dict(event))


def new_events_since(
    events: Sequence[Mapping[str, Any]],
    controller: Mapping[str, Any],
    *,
    run_id: str,
    rollover_window: int = 64,
) -> List[Mapping[str, Any]]:
    """Return events appended after the controller's opaque tail cursor.

    ``agent_events`` is capped, so its length is not a monotonic offset.  A
    cursor normally survives the rolling window and identifies the exact tail.
    If more than the entire buffer turns over between samples, conservatively
    replay a bounded tail; ledger signatures make that replay idempotent enough
    to resume observation without permanently dropping new events.
    """
    current = list(events)
    if str(controller.get("run_id") or "") != run_id:
        return current

    cursor = str(controller.get("processed_event_cursor") or "")
    if cursor:
        for index in range(len(current) - 1, -1, -1):
            if event_fingerprint(current[index]) == cursor:
                unseen = current[index + 1 :]
                if unseen:
                    return unseen
                break
        else:
            return current[-max(1, min(int(rollover_window), len(current))) :]

    processed_count = as_int(controller.get("processed_event_count"))
    start = max(0, min(processed_count, len(current)))
    unseen = current[start:]
    if unseen:
        return unseen

    # A capped buffer can keep the same length while replacing its oldest
    # entries. If the cycle advanced but the prior cursor was unavailable (for
    # example after migration), replay a bounded tail instead of going blind.
    if (
        len(current) >= 1000
        and as_int(controller.get("last_cycle")) < as_int(current[-1].get("cycle"))
    ):
        return current[-max(1, min(int(rollover_window), len(current))) :]
    return []


def progress_observation(task: Mapping[str, Any]) -> Dict[str, Any]:
    progress = task.get("agent_progress_state") if isinstance(task.get("agent_progress_state"), dict) else {}
    observation = progress.get("observation") if isinstance(progress.get("observation"), dict) else {}
    return dict(observation)


def durable_state_components(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Return output-bearing state only.

    Plan and guidance revisions are intentionally excluded. They are useful
    context, but neither proves that the workspace, validation, review, or
    terminal state changed. This prevents note-only churn from minting fresh
    recovery opportunities.
    """
    observation = progress_observation(task)
    return {
        "workspace_fingerprint": str(observation.get("workspace_fingerprint") or ""),
        "validation_revision": as_int(observation.get("validation_revision")),
        "diff_review_revision": as_int(observation.get("diff_review_revision")),
        "finish_state": str(observation.get("finish_state") or "running"),
    }


def durable_state_key(task: Mapping[str, Any]) -> str:
    return stable_hash(durable_state_components(task))


def current_run_events(task: Mapping[str, Any]) -> List[Dict[str, Any]]:
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    run_id = str(task.get("agent_run_id") or "").strip()
    start_index = 0
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if str(event.get("type") or "") != "started":
            continue
        event_run_id = str(event.get("run_id") or "").strip()
        if not run_id or not event_run_id or event_run_id == run_id:
            start_index = index
            break
    return events[start_index:]


def _path(value: Any, *, default: str = ".") -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"/+", "/", text).strip("/")
    return text or default


def _query_tokens(value: Any) -> List[str]:
    tokens = [token.casefold() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(value or ""))]
    return sorted({token for token in tokens if token not in _STOPWORDS})[:6]


def inspection_target(event: Mapping[str, Any]) -> str:
    name = str(event.get("name") or "").strip()
    if name not in _INSPECTION_TOOLS and name not in _VALIDATION_TOOLS:
        return ""
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    if name == "coding_search_text":
        path = _path(args.get("path"))
        query = clip(args.get("query"), 120)
        return f"search {path}: {query}" if query else f"search {path}"
    if name in {"coding_read_file", "coding_read_file_lines"}:
        path = _path(args.get("path"), default="")
        if not path:
            return name
        start = as_int(args.get("start_line"))
        count = as_int(args.get("line_count"))
        suffix = f" lines {start}-{start + count - 1}" if start > 0 and count > 0 else ""
        return f"read {path}{suffix}"
    if name == "coding_list_tree":
        return f"list {_path(args.get('path'))}"
    if name == "coding_run_command":
        argv = args.get("argv") if isinstance(args.get("argv"), list) else []
        normalized = [str(item).strip() for item in argv if str(item).strip()]
        command = _path(normalized[0], default="command").rsplit("/", 1)[-1] if normalized else "command"
        # Never persist raw argv: validation commands commonly contain headers,
        # tokens, or passwords. The basename is useful orientation and the
        # opaque hash preserves semantic grouping without exposing arguments.
        return f"validate {clip(command, 80)} argv:{stable_hash(normalized)[:16]}"
    return name.removeprefix("coding_")


def inspection_signature(event: Mapping[str, Any]) -> str:
    name = str(event.get("name") or "").strip()
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    if name == "coding_search_text":
        path = _path(args.get("path"))
        return f"search:{path}:{','.join(_query_tokens(args.get('query')))}"
    if name in {"coding_read_file", "coding_read_file_lines"}:
        return f"read:{_path(args.get('path'), default='(unknown)')}"
    if name == "coding_list_tree":
        return f"tree:{_path(args.get('path'))}"
    if name in _REVIEW_TOOLS:
        return f"review:{name}"
    if name == "coding_run_command":
        argv = args.get("argv") if isinstance(args.get("argv"), list) else []
        normalized = [str(item).strip() for item in argv if str(item).strip()]
        return "validate:" + stable_hash(normalized)[:16]
    return ""


def update_inspection_ledger(
    existing: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    cycle: int,
    limit: int = 32,
) -> List[Dict[str, Any]]:
    ledger: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in existing if isinstance(existing, list) else []:
        if not isinstance(raw, dict):
            continue
        signature = str(raw.get("signature") or "").strip()
        if not signature:
            continue
        ledger[signature] = dict(raw)
        order.append(signature)
    for event in events:
        if str(event.get("type") or "") != "tool_started":
            continue
        signature = inspection_signature(event)
        target = inspection_target(event)
        if not signature or not target:
            continue
        entry = ledger.get(signature, {"signature": signature, "count": 0})
        entry.update({
            "target": target,
            "count": as_int(entry.get("count")) + 1,
            "last_run_id": run_id,
            "last_cycle": cycle,
            "last_seen_at": float(event.get("ts") or time.time()),
        })
        ledger[signature] = entry
        if signature in order:
            order.remove(signature)
        order.append(signature)
    return [ledger[key] for key in order[-max(1, limit):]]


def _tool_names(events: Iterable[Mapping[str, Any]]) -> List[str]:
    return [str(event.get("name") or "") for event in events if str(event.get("type") or "") == "tool_started"]


def classify_stagnation(task: Mapping[str, Any], events: Sequence[Mapping[str, Any]], ledger: Sequence[Mapping[str, Any]]) -> str:
    names = _tool_names(events)
    inspection_names = [name for name in names if name in _INSPECTION_TOOLS]
    edit_names = [name for name in names if name in _EDIT_TOOLS]
    validation_names = [name for name in names if name in _VALIDATION_TOOLS]
    review_names = [name for name in names if name in _REVIEW_TOOLS]
    recent = [item for item in ledger[-8:] if isinstance(item, dict)]
    repeated = sum(max(0, as_int(item.get("count")) - 1) for item in recent)
    unique_families = {str(item.get("signature") or "").split(":", 1)[0] for item in recent}
    if len(inspection_names) >= 4 and not edit_names and (repeated >= 2 or len(unique_families) <= 2):
        return "inspection_loop"
    if len(review_names) >= 3 and not edit_names:
        return "review_loop"
    if len(validation_names) >= 3 and not edit_names:
        return "validation_loop"
    if not names and any(str(item.get("type") or "") == "assistant" for item in events):
        return "reasoning_loop"
    observation = progress_observation(task)
    plan_revision = as_int(observation.get("plan_revision"))
    controller = task.get("agent_stagnation_controller") if isinstance(task.get("agent_stagnation_controller"), dict) else {}
    if plan_revision > as_int(controller.get("plan_revision")) + 1 and not edit_names and not validation_names and not review_names:
        return "plan_churn"
    return "stagnant_execution"


def stage_thresholds(max_no_progress_cycles: int) -> StageThresholds:
    terminal = max(2, int(max_no_progress_cycles))
    assist = max(1, min(3, terminal - 2))
    interrupt = min(max(assist + 1, min(5, terminal - 1)), terminal)
    return StageThresholds(assist=assist, interrupt=interrupt, terminal=terminal)


def stage_for_cycles(cycles: int, thresholds: StageThresholds) -> str:
    if cycles >= thresholds.terminal:
        return "recovery"
    if cycles >= thresholds.interrupt:
        return "interrupt"
    if cycles >= thresholds.assist:
        return "assist"
    return "observe"


def advance_controller(
    task: Mapping[str, Any], *, state_key: str, run_id: str, cycle: int,
    progress_stagnant_cycles: int, classification: str, max_no_progress_cycles: int,
) -> Dict[str, Any]:
    raw = task.get("agent_stagnation_controller") if isinstance(task.get("agent_stagnation_controller"), dict) else {}
    previous_state_key = str(raw.get("state_key") or "")
    previous_run_id = str(raw.get("run_id") or "")
    previous_cycle = as_int(raw.get("last_cycle"))
    same_sample = previous_run_id == run_id and previous_cycle == cycle
    if previous_state_key != state_key:
        cycles = max(0, progress_stagnant_cycles)
    elif same_sample:
        cycles = max(as_int(raw.get("cycles")), progress_stagnant_cycles)
    else:
        cycles = max(as_int(raw.get("cycles")) + 1, progress_stagnant_cycles)
    thresholds = stage_thresholds(max_no_progress_cycles)
    return {
        "schema": SCHEMA,
        "state_key": state_key,
        "run_id": run_id,
        "previous_run_id": previous_run_id,
        "last_cycle": cycle,
        "cycles": cycles,
        "progress_stagnant_cycles": progress_stagnant_cycles,
        "classification": classification,
        "stage": stage_for_cycles(cycles, thresholds),
        "thresholds": {"assist": thresholds.assist, "interrupt": thresholds.interrupt, "terminal": thresholds.terminal},
        "plan_revision": as_int(progress_observation(task).get("plan_revision")),
        "interventions": [item for item in (raw.get("interventions") or []) if isinstance(item, dict)][-24:],
        "updated_at": time.time(),
    }


def _recent_assistant_notes(events: Sequence[Mapping[str, Any]], *, limit: int = 3) -> List[str]:
    notes: List[str] = []
    seen = set()
    for event in reversed(events):
        if str(event.get("type") or "") != "assistant":
            continue
        note = clip(event.get("content"), 500)
        if note and note.casefold() not in seen:
            seen.add(note.casefold())
            notes.append(note)
            if len(notes) >= limit:
                break
    notes.reverse()
    return notes


_COMMITMENT_PATTERN = re.compile(
    r"(?:\bI(?:'ll| will| need to| should)\b|\b(?:now )?let me\b|\bnext(?:,? I(?:'ll| will))?\b)"
    r"(?:(?![.!?\n]).){0,180}?\b"
    r"(add|write|implement|fix|edit|update|remove|create|patch|run|finish)\b\s+"
    r"([^.!?\n]{3,260})",
    re.IGNORECASE,
)
_GENERIC_ACTION_PREFIXES = (
    "take one bounded action",
    "stop broad inspection",
    "convert the review finding",
    "fix one concrete validation failure",
    "stop revising notes",
    "call one workspace tool",
)


def extract_concrete_commitment(events: Sequence[Mapping[str, Any]]) -> str:
    for event in reversed(events):
        if str(event.get("type") or "") != "assistant":
            continue
        text = " ".join(str(event.get("content") or "").split())
        if not text:
            continue
        matches = list(_COMMITMENT_PATTERN.finditer(text))
        if not matches:
            continue
        match = matches[-1]
        verb = match.group(1).lower()
        target = match.group(2).strip(" `:;- ")
        target = re.split(r"\b(?:before|after|then)\b", target, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,;-")
        if len(target) < 3:
            continue
        return clip(f"{verb.capitalize()} {target}.", 700)
    return ""



def pending_concrete_commitment(events: Sequence[Mapping[str, Any]]) -> str:
    # A commitment is pending only until the model attempts a workspace tool.
    action_seen = False
    for event in reversed(events):
        event_type = str(event.get("type") or "")
        if event_type == "tool_started":
            action_seen = True
            continue
        if event_type != "assistant":
            continue
        commitment = extract_concrete_commitment([event])
        if not commitment:
            continue
        return "" if action_seen else commitment
    return ""


def mission_requires_file_changes(task: Mapping[str, Any]) -> bool:
    mission = task.get("mission") if isinstance(task.get("mission"), dict) else {}
    completion = mission.get("completion_policy") if isinstance(mission.get("completion_policy"), dict) else {}
    return bool(completion.get("require_file_changes", True))


def action_kind_for_required_action(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return "bounded"
    if "coding_finish" in normalized or normalized.startswith(("finish", "conclude", "report the review")):
        return "finish"
    if "coding_git_diff" in normalized or normalized.startswith(("review the diff", "inspect the diff")):
        return "diff_review"
    if normalized.startswith(("run ", "rerun ", "validate ", "test ", "check ")):
        return "validate"
    if normalized.startswith(("add ", "write ", "implement ", "fix ", "edit ", "update ", "remove ", "create ", "patch ")):
        return "edit"
    return "bounded"

def generic_next_action(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
    return not normalized or any(normalized.startswith(prefix) for prefix in _GENERIC_ACTION_PREFIXES)


def active_plan_summary(task: Mapping[str, Any]) -> str:
    plan = task.get("project_plan") if isinstance(task.get("project_plan"), dict) else {}
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    active = next((item for item in items if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "in_progress"), None)
    if not isinstance(active, dict):
        return ""
    return clip(f"{active.get('title') or ''}: {active.get('summary') or ''}", 600)


def build_working_memory(
    task: Mapping[str, Any], *, state_key: str, controller: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    previous = task.get("agent_working_memory") if isinstance(task.get("agent_working_memory"), dict) else {}
    findings: List[str] = []
    for value in previous.get("findings") if isinstance(previous.get("findings"), list) else []:
        text = clip(value, 500)
        if text and text not in findings:
            findings.append(text)
    active_plan = active_plan_summary(task)
    if active_plan and active_plan not in findings:
        findings.append(active_plan)
    for note in _recent_assistant_notes(events):
        if note not in findings:
            findings.append(note)
    findings = findings[-6:]
    inspected = [clip(item.get("target"), 240) for item in ledger[-12:] if isinstance(item, dict)]
    classification = str(controller.get("classification") or "stagnant_execution")
    stage = str(controller.get("stage") or "observe")
    review_only = not mission_requires_file_changes(task)
    enforced_stage = stage in {"interrupt", "recovery", "continuation"}
    defaults = {
        "inspection_loop": ("Which already-established finding identifies the smallest viable edit or a concrete blocker?", "Stop broad inspection. Make the smallest evidence-backed edit, or finish with a concrete blocker."),
        "review_loop": ("What unresolved defect remains after the repeated status/diff review?", "Convert the review finding into one edit or finish with a concrete blocker."),
        "validation_loop": ("Which validation failure is actionable and not already explained by the current workspace state?", "Fix one concrete validation failure, then rerun only the targeted check."),
        "plan_churn": ("Which plan statement corresponds to an observable repository transition?", "Stop revising notes. Execute the current plan item or finish with a blocker."),
        "reasoning_loop": ("What concrete repository action follows from the current reasoning?", "Call one workspace tool that edits, validates, reviews an edit, or finishes with a blocker."),
        "stagnant_execution": ("What concrete action will change workspace, validation, review, or terminal state?", "Take one bounded action that changes durable state, or finish with a concrete blocker."),
    }
    if review_only and enforced_stage:
        unresolved_default = (
            "Which concrete findings or validation results are sufficiently supported to report, "
            "and which remain environment-dependent?"
        )
        next_default = (
            "Call coding_finish with the concrete review findings and validation evidence already collected, "
            "clearly distinguishing confirmed defects from environment or configuration blockers; "
            "if none are verified, state that no actionable defect was found."
        )
    elif review_only:
        review_defaults = {
            "inspection_loop": ("Which already-inspected target can resolve the review with one bounded follow-up?", "Inspect one already-identified target, or finish with the evidence already collected."),
            "review_loop": ("Which concrete finding remains unresolved after the repeated review?", "Resolve one concrete finding, or finish the review."),
            "validation_loop": ("Which validation result is most informative for the review conclusion?", "Perform one targeted follow-up on the most informative validation result, or finish with the evidence already collected."),
            "plan_churn": ("Which review question can be resolved by one repository action?", "Stop revising notes. Perform one bounded review action or finish."),
            "reasoning_loop": ("What concrete review action follows from the current reasoning?", "Perform one bounded review action or finish with a concrete conclusion."),
            "stagnant_execution": ("What evidence remains necessary before concluding the review?", "Take one bounded review action or finish with the current findings."),
        }
        unresolved_default, next_default = review_defaults.get(classification, review_defaults["stagnant_execution"])
    else:
        unresolved_default, next_default = defaults.get(classification, defaults["stagnant_execution"])
    previous_directives = previous if str(previous.get("state_key") or "") == state_key else {}
    commitment = pending_concrete_commitment(events)
    next_action = commitment or next_default
    action_source = "assistant_commitment" if commitment else "controller_default"
    unresolved = unresolved_default
    if action_source == "assistant_commitment":
        unresolved = clip(f"What remains before executing this commitment: {commitment}", 700)
    next_action = clip(next_action, 700)
    next_action_kind = action_kind_for_required_action(next_action)
    blocker = clip(previous_directives.get("blocker"), 700)
    blocker = clip(previous_directives.get("blocker"), 700)
    content = {
        "state_key": state_key, "findings": findings, "inspected_targets": inspected,
        "unresolved_question": unresolved, "next_action": next_action,
        "next_action_kind": next_action_kind,
        "required_action_source": action_source,
        "blocker": blocker, "classification": classification,
    }
    content_fingerprint = stable_hash(content)
    revision = as_int(previous.get("revision")) + (1 if content_fingerprint != str(previous.get("content_fingerprint") or "") else 0)
    return {
        "schema": WORKING_MEMORY_SCHEMA,
        "revision": max(1, revision),
        "run_id": str(task.get("agent_run_id") or ""),
        "cycle": as_int(task.get("agent_cycle")),
        "state_key": state_key,
        "classification": classification,
        "stage": str(controller.get("stage") or "observe"),
        "findings": findings,
        "inspected_targets": inspected,
        "unresolved_question": unresolved,
        "next_action": next_action,
        "next_action_kind": next_action_kind,
        "required_action_source": action_source,
        "blocker": blocker,
        "content_fingerprint": content_fingerprint,
        "provenance": {
            "findings": "project_plan and recent assistant notes; assistant notes remain unverified",
            "inspected_targets": "controller-normalized tool_started events across runs",
            "state": "workspace, validation, diff-review, and finish observations",
        },
        "updated_at": time.time(),
    }


def build_context_manifest(
    task: Mapping[str, Any], *, state_key: str, working_memory: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    all_events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    kept = list(events[-24:])
    manifest = {
        "schema": CONTEXT_MANIFEST_SCHEMA,
        "state_key": state_key,
        "run_id": str(task.get("agent_run_id") or ""),
        "cycle": as_int(task.get("agent_cycle")),
        "source_event_count": len(all_events),
        "preserved_event_count": len(kept),
        "omitted_event_count": max(0, len(all_events) - len(kept)),
        "source_event_range": {
            "first_ts": float(kept[0].get("ts") or 0) if kept else 0.0,
            "last_ts": float(kept[-1].get("ts") or 0) if kept else 0.0,
        },
        "preserved_sections": [
            "durable output state", "working memory", "inspection ledger",
            "unresolved question", "exactly one next action", "concrete blocker",
        ],
        "working_memory_revision": as_int(working_memory.get("revision")),
        "generated_at": time.time(),
    }
    manifest["manifest_hash"] = stable_hash(
        {key: value for key, value in manifest.items() if key != "generated_at"}
    )
    return manifest


def previous_run_stop_reason(task: Mapping[str, Any]) -> str:
    previous_run_id = str(task.get("agent_previous_run_id") or "").strip()
    if previous_run_id:
        for run in reversed(task.get("agent_runs") or []):
            if not isinstance(run, Mapping):
                continue
            if str(run.get("run_id") or "").strip() != previous_run_id:
                continue
            reason = str(run.get("stop_reason_code") or "").strip()
            if reason:
                return reason
            break
    direct = str(task.get("agent_previous_stop_reason_code") or "").strip()
    if direct:
        return direct
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
    return str(task.get("agent_stop_reason_code") or terminal.get("stop_reason_code") or "").strip()


def _new_controller_run(controller: Mapping[str, Any]) -> bool:
    previous_run_id = str(controller.get("previous_run_id") or "")
    run_id = str(controller.get("run_id") or "")
    return bool(previous_run_id and previous_run_id != run_id)


def continuation_after_no_progress(task: Mapping[str, Any], controller: Mapping[str, Any]) -> bool:
    if not _new_controller_run(controller):
        return False
    reason = previous_run_stop_reason(task)
    previous_status = str(task.get("agent_previous_status") or "").strip().lower()
    previous_summary = str(task.get("agent_previous_summary") or "").lower()
    return reason == "no_progress_limit" or (previous_status == "paused" and "without a durable state transition" in previous_summary)


def continuation_after_gateway_interruption(
    task: Mapping[str, Any],
    controller: Mapping[str, Any],
) -> bool:
    if not _new_controller_run(controller):
        return False
    previous_status = str(task.get("agent_previous_status") or "").strip().lower()
    return previous_status == "interrupted" and previous_run_stop_reason(task) == "gateway_stopped"


def intervention_kind(task: Mapping[str, Any], controller: Mapping[str, Any]) -> str:
    if continuation_after_no_progress(task, controller):
        return "continuation"
    return str(controller.get("stage") or "observe")


def intervention_id(state_key: str, kind: str, *, run_id: str = "") -> str:
    base = f"{state_key}:{kind}"
    return f"{base}:{run_id}" if run_id else base


def intervention_already_claimed(controller: Mapping[str, Any], intervention: str) -> bool:
    return any(isinstance(item, dict) and str(item.get("id") or "") == intervention for item in (controller.get("interventions") or []))


def append_intervention(
    controller: Mapping[str, Any], *, intervention: str, kind: str,
    run_id: str, cycle: int,
) -> Dict[str, Any]:
    updated = dict(controller)
    history = [item for item in (controller.get("interventions") or []) if isinstance(item, dict)]
    history.append({"id": intervention, "kind": kind, "run_id": run_id, "cycle": cycle, "claimed_at": time.time()})
    updated["interventions"] = history[-24:]
    updated["last_intervention_id"] = intervention
    updated["last_intervention_kind"] = kind
    updated["updated_at"] = time.time()
    return updated


def render_guidance(
    controller: Mapping[str, Any], working_memory: Mapping[str, Any],
    manifest: Mapping[str, Any], *, kind: str,
) -> str:
    classification = str(controller.get("classification") or "stagnant_execution")
    stage = str(controller.get("stage") or kind)
    lines = [
        "Controller investigation checkpoint (stagnation resilience; authoritative across compaction, restart, and continuation):",
        f"- Classification: {classification}.",
        f"- Intervention stage: {stage}; controller no-outcome cycles: {as_int(controller.get('cycles'))}.",
        f"- Durable state key: {controller.get('state_key') or ''}.",
        f"- Unresolved question: {working_memory.get('unresolved_question') or ''}",
        f"- Required next action: {working_memory.get('next_action') or ''}",
        "- Exactly one next action is authorized. Broad orientation, note-only plan churn, and repeated adjacent reads do not count as progress.",
        "- Progress requires an observable workspace edit, validation transition, diff-review transition, or terminal finish/blocker decision.",
    ]
    blocker = str(working_memory.get("blocker") or "").strip()
    if blocker:
        lines.append(f"- Current blocker: {blocker}")
    findings = working_memory.get("findings") if isinstance(working_memory.get("findings"), list) else []
    if findings:
        lines.append("- Established working memory (assistant-derived items remain unverified until repository evidence confirms them):")
        lines.extend(f"  - {item}" for item in findings)
    inspected = working_memory.get("inspected_targets") if isinstance(working_memory.get("inspected_targets"), list) else []
    if inspected:
        lines.append("- Already inspected; do not repeat unless directly necessary for the required action:")
        lines.extend(f"  - {item}" for item in inspected)
    lines.append(
        f"- Context manifest: preserved {as_int(manifest.get('preserved_event_count'))} events and omitted "
        f"{as_int(manifest.get('omitted_event_count'))}; working-memory revision {as_int(working_memory.get('revision'))}."
    )
    if kind == "interrupt":
        lines.append("- Interruption rule: the next cycle must edit, validate/review an existing edit, or finish with a concrete blocker.")
    elif kind == "recovery":
        lines.append("- Recovery rule: this durable state receives one recovery intervention only; unchanged repetition will terminate normally.")
    elif kind == "continuation":
        lines.append("- Continuation rule: the prior no-progress investigation is exhausted. Do not reconstruct it; act on the required next action immediately.")
    return "\n".join(lines)
