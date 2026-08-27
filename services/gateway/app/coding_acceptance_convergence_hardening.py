from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, Mapping


_TERMINAL_SCHEMA = "nexus_coding_terminal_convergence.v1"
_TERMINAL_ACTION = "finish"
_VALIDATION_KEY = "coding_validation_provenance"
_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
_SEMANTIC_STATE_EVENT = "semantic_acceptance_state"
_REJECTION_GUARD_KEY = "coding_semantic_rejection_guard"
_VALIDATION_HISTORY_LIMIT = 32
_MISSING_TOOL_MARKERS = (
    "command not found:",
    "no module named pytest",
    "no module named ruff",
    "no module named mypy",
    "modulenotfounderror: no module named",
    "executable file not found",
    "not recognized as an internal or external command",
)
_HYPOTHESIS_FIELDS = (
    "Root cause",
    "Repository evidence",
    "Competing explanation checked",
    "Expected result",
)
_HYPOTHESIS_FIELD_RE = re.compile(
    rf"(?is)(?:^|[\n;])\s*({'|'.join(re.escape(label) for label in _HYPOTHESIS_FIELDS)})\s*:\s*"
)
_UNKNOWN_LINE_FIELD_RE = re.compile(r"(?m)^\s*([^:\n]{1,120})\s*:\s*")
_BOOKKEEPING_LABEL_RE = re.compile(
    r"(?i)(?:^|\b)(status|state|progress|note|next action|next step|blocker|phase|stage|result)(?:\b|\s|\()"
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _note_fingerprint(note: Any) -> str:
    # coding_completion_state_hardening strips the persisted hypothesis note
    # before recording its lifecycle fingerprint. Keep comparison semantics
    # identical so whitespace-only plan rewrites do not look causal.
    normalized = str(note or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _looks_like_trailing_bookkeeping_label(label: str) -> bool:
    normalized = str(label or "").strip()
    if not normalized:
        return False
    if any(char in normalized for char in ("/", "\\")):
        return False
    # File/symbol locators such as app.py: or package.module: are ordinary
    # hypothesis value content, not plan-section boundaries.
    if "." in normalized and not normalized.endswith("."):
        return False
    if _BOOKKEEPING_LABEL_RE.search(normalized):
        return True
    if "(" in normalized or ")" in normalized:
        return True
    # Non-ASCII plan labels are commonly generated bookkeeping headings. Keep
    # them suffix-only so non-ASCII prose inside earlier fields remains intact.
    return any(ord(char) > 127 for char in normalized)


def _structured_hypothesis_fields(note: Any) -> Dict[str, str]:
    """Parse only the four causal fields without truncating locator-like lines.

    Only the four known field labels delimit values, so unknown ``label:`` lines
    inside a field (multiline repository evidence such as ``app.py: ...``)
    remain value content. After the final known field, a clearly
    bookkeeping-like suffix section (status/state/progress and similar) is
    stripped so trailing plan bookkeeping never becomes causal state.
    """
    text = str(note or "").strip()
    if not text:
        return {}
    matches = list(_HYPOTHESIS_FIELD_RE.finditer(text))
    if not matches:
        return {}
    seen_matches: list[tuple[str, "re.Match[str]"]] = []
    seen_labels: set[str] = set()
    for match in matches:
        raw_label = str(match.group(1) or "").strip()
        label = next(
            (
                candidate
                for candidate in _HYPOTHESIS_FIELDS
                if candidate.casefold() == raw_label.casefold()
            ),
            "",
        )
        if label and label not in seen_labels:
            seen_labels.add(label)
            seen_matches.append((label, match))
    if seen_labels != set(_HYPOTHESIS_FIELDS):
        return {}

    fields: Dict[str, str] = {}
    for index, (label, match) in enumerate(seen_matches):
        end = seen_matches[index + 1][1].start() if index + 1 < len(seen_matches) else len(text)
        if index + 1 == len(seen_matches):
            # Only the final field is eligible for a non-causal suffix. Locator
            # lines remain content; status/state-style labels terminate it.
            for candidate in _UNKNOWN_LINE_FIELD_RE.finditer(text, match.end(), end):
                candidate_label = str(candidate.group(1) or "").strip()
                if any(
                    known.casefold() == candidate_label.casefold()
                    for known in _HYPOTHESIS_FIELDS
                ):
                    continue
                if _looks_like_trailing_bookkeeping_label(candidate_label):
                    end = candidate.start()
                    break
        value = text[match.end() : end].strip(" \t\r\n;")
        if not value:
            return {}
        fields[label] = value
    return fields


def _structured_hypothesis_fingerprint_from_note(note: Any) -> str:
    fields = _structured_hypothesis_fields(note)
    if not fields:
        return ""
    payload = "v2\x1f" + "\x1f".join(
        f"{label}\x1e{fields[label]}" for label in _HYPOTHESIS_FIELDS
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _structured_hypothesis_fingerprint(task: Mapping[str, Any]) -> str:
    plan = _mapping(task.get("project_plan"))
    return _structured_hypothesis_fingerprint_from_note(plan.get("note"))


def _material_hypothesis_updated_at(task: Mapping[str, Any]) -> float:
    """Return plan update time only for a replacement four-field hypothesis.

    Project-plan bookkeeping is intentionally broader than causal state: item
    status, summaries, and generic notes may all advance ``updated_at`` after
    validation and diff review. Those updates must not reopen broad tools. New
    lifecycle records persist a structured hypothesis fingerprint so trailing
    status sections can change without becoming causal changes. Legacy records
    fall back to the exact normalized note fingerprint that existed before this
    convergence layer.
    """
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    if str(lifecycle.get("status") or "") != "consumed":
        return 0.0

    plan = _mapping(task.get("project_plan"))
    current_structured = _structured_hypothesis_fingerprint(task)
    if not current_structured:
        return 0.0

    # Tolerate fingerprints recorded under earlier parser revisions: compare
    # against the stored value and against a recomputation of the consumed note
    # under the current parser, so a parser change never manufactures a
    # "material" hypothesis update for an unchanged note.
    consumed_candidates = {
        str(lifecycle.get("structured_hypothesis_fingerprint") or "").strip(),
    }
    consumed_note = str(lifecycle.get("consumed_hypothesis_note") or "").strip()
    if consumed_note:
        consumed_candidates.add(_structured_hypothesis_fingerprint_from_note(consumed_note))
    consumed_candidates.discard("")
    if current_structured in consumed_candidates:
        return 0.0

    if not consumed_candidates:
        # Legacy lifecycle records carry only the exact-note fingerprint that
        # existed before this convergence layer. Preserve that fallback rather
        # than manufacturing a causal change.
        consumed_note_fp = str(lifecycle.get("note_fingerprint") or "").strip()
        current_note = _note_fingerprint(plan.get("note"))
        if not consumed_note_fp or current_note == consumed_note_fp:
            return 0.0

    plan_revision = _int(plan.get("revision"))
    consumed_revision = _int(lifecycle.get("plan_revision"))
    if plan_revision <= consumed_revision:
        return 0.0
    return _float(plan.get("updated_at"))


def _readiness_threshold(task: Mapping[str, Any], mission_epoch: Any) -> float:
    epoch = _mapping(task.get(getattr(mission_epoch, "KEY", "coding_mission_acceptance_epoch")))
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    return max(
        _float(epoch.get("last_mutation_at")),
        _float(lifecycle.get("consumed_at")),
        _material_hypothesis_updated_at(task),
    )


def _validation_signature(argv: Any) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in argv if str(item).strip())


def _validation_result_missing_tool(result: Mapping[str, Any]) -> bool:
    if result.get("ok") is True:
        return False
    text = (
        f"{result.get('error') or ''}\n"
        f"{result.get('stdout') or ''}\n"
        f"{result.get('stderr') or ''}"
    ).lower()
    return any(marker in text for marker in _MISSING_TOOL_MARKERS)


def _validation_records_from_history(
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...], bool]]:
    validation = _mapping(task.get(_VALIDATION_KEY))
    output: list[tuple[float, tuple[str, ...], bool]] = []
    for raw in validation.get("history") or []:
        item = _mapping(raw)
        ts = _float(item.get("ts"))
        signature = _validation_signature(item.get("argv"))
        if ts < threshold or not signature or item.get("substantive") is False:
            continue
        output.append((ts, signature, item.get("ok") is True))
    return output


def _validation_records_from_events(
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...], bool]]:
    from app import coding_work_phases

    pending_by_id: dict[str, tuple[tuple[str, ...] | None, float]] = {}
    pending_without_id: list[tuple[tuple[str, ...] | None, float]] = []
    output: list[tuple[float, tuple[str, ...], bool]] = []
    for raw in task.get("agent_events") or []:
        event = _mapping(raw)
        event_type = str(event.get("type") or "")
        name = str(event.get("name") or "")
        call_id = str(event.get("tool_call_id") or "").strip()
        if event_type == "tool_started" and name == "coding_run_command":
            # Track every start (validation or not) so id-less finished events
            # can never steal an unrelated validation start from the FIFO.
            args = _mapping(event.get("args"))
            argv = args.get("argv")
            signature = (
                _validation_signature(argv)
                if coding_work_phases.is_validation_command(argv)
                else None
            )
            started = (signature, _float(event.get("ts")))
            if call_id:
                pending_by_id[call_id] = started
            else:
                pending_without_id.append(started)
            continue
        if event_type != "tool_finished" or name != "coding_run_command":
            continue
        started = pending_by_id.pop(call_id, None) if call_id else (
            pending_without_id.pop(0) if pending_without_id else None
        )
        if started is None:
            continue
        signature, started_at = started
        if not signature:
            continue
        result = _mapping(event.get("result"))
        if str(result.get("error") or "") == "forced_action_tool_rejected":
            continue
        if _validation_result_missing_tool(result):
            continue
        finished_at = _float(event.get("ts")) or started_at
        if finished_at < threshold:
            continue
        output.append((finished_at, signature, result.get("ok") is True))
    return output


def _validation_records_from_commands(
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...], bool]]:
    from app import coding_work_phases

    output: list[tuple[float, tuple[str, ...], bool]] = []
    for raw in task.get("commands") or []:
        item = _mapping(raw)
        if str(item.get("label") or "") not in {"agent-command", "command"}:
            continue
        argv = item.get("argv")
        if not coding_work_phases.is_validation_command(argv):
            continue
        ts = _float(item.get("ts"))
        signature = _validation_signature(argv)
        if ts < threshold or not signature:
            continue
        if _validation_result_missing_tool(item):
            continue
        output.append((ts, signature, item.get("ok") is True))
    return output


def _validation_records(
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...], bool]]:
    """Single source for post-threshold validation records across all ledgers."""
    records = [
        *_validation_records_from_history(task, threshold),
        *_validation_records_from_events(task, threshold),
        *_validation_records_from_commands(task, threshold),
    ]
    durable = _mapping(task.get(_VALIDATION_KEY))
    durable_ts = _float(durable.get("ts"))
    durable_signature = _validation_signature(durable.get("argv"))
    durable_substantive = durable.get("substantive") is not False
    if durable_ts >= threshold and durable_signature and durable_substantive:
        record = (durable_ts, durable_signature, durable.get("ok") is True)
        if record not in records:
            records.append(record)
    unique = {record: record for record in records}
    return sorted(unique.values(), key=lambda item: item[0])


def _unresolved_validation_failures(
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...]]]:
    """Substantive failures not yet superseded by a same-signature success."""
    records = _validation_records(task, threshold)
    failures: list[tuple[float, tuple[str, ...]]] = []
    for failed_at, failed_signature, ok in records:
        if ok:
            continue
        superseded = any(
            later_at > failed_at and later_signature == failed_signature and later_ok
            for later_at, later_signature, later_ok in records
        )
        if not superseded:
            failures.append((failed_at, failed_signature))
    return failures


def _validation_obligations_ready(task: Mapping[str, Any], threshold: float) -> bool:
    """Do not let a later weak check erase an unresolved substantive failure.

    The original finish gate enforced this rule inside one runner attempt. This
    terminal layer is mission-scoped, so it must preserve the same obligation
    across checkpoints and resumes before semantic acceptance can run.
    """
    return not _unresolved_validation_failures(task, threshold)


def _validation_ready(task: Mapping[str, Any], threshold: float) -> tuple[bool, float]:
    validation = _mapping(task.get(_VALIDATION_KEY))
    ts = _float(validation.get("ts"))
    ready = bool(
        str(validation.get("schema") or "") == "nexus_coding_validation_provenance.v1"
        and validation.get("ok") is True
        and ts
        and ts >= threshold
        and _validation_obligations_ready(task, threshold)
    )
    return ready, ts


def _latest_diff_review_at(task: Mapping[str, Any], threshold: float) -> float:
    for raw in reversed(list(task.get("agent_events") or [])):
        event = _mapping(raw)
        if str(event.get("type") or "") != "tool_finished":
            continue
        if str(event.get("name") or "") != "coding_git_diff":
            continue
        result = _mapping(event.get("result"))
        if result.get("ok") is False or str(result.get("error") or "").strip():
            continue
        ts = _float(event.get("ts"))
        if ts and ts >= threshold:
            return ts
    return 0.0


def _latest_decisive_rejection(task: Mapping[str, Any], threshold: float) -> Mapping[str, Any]:
    """Return a durable semantic rejection for the current readiness epoch.

    Retryable reviewer/protocol failures do not erase an earlier decisive
    rejection. Only a later causal-state threshold (new mutation or replacement
    hypothesis) supersedes the rejection.
    """
    for raw in reversed(list(task.get("agent_events") or [])):
        event = _mapping(raw)
        if str(event.get("type") or "") != _SEMANTIC_STATE_EVENT:
            continue
        if _float(event.get("ts")) < threshold:
            continue
        if event.get("accepted") is False and not bool(event.get("review_error")):
            return event
    return {}


def _hypothesis_identity(task: Mapping[str, Any]) -> str:
    current = _structured_hypothesis_fingerprint(task)
    if current:
        return current
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    return str(
        lifecycle.get("structured_hypothesis_fingerprint")
        or lifecycle.get("note_fingerprint")
        or ""
    ).strip()


def _semantic_rejection_guard_key(
    cw: Any,
    mission_epoch: Any,
    task_id: str,
    task: Mapping[str, Any],
) -> str:
    try:
        delta = mission_epoch.mission_delta_state(cw, task_id, dict(task))
    except Exception:
        return ""
    if not delta.get("ok") or not delta.get("has_delta"):
        return ""
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    payload = "\x1f".join(
        [
            str(delta.get("diff_sha256") or ""),
            _hypothesis_identity(task),
            str(lifecycle.get("verified_evidence_digest") or ""),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _semantic_rejection_guard_blocks(
    cw: Any,
    mission_epoch: Any,
    task_id: str,
    task: Mapping[str, Any],
) -> bool:
    guard = _mapping(task.get(_REJECTION_GUARD_KEY))
    recorded = str(guard.get("causal_key") or "").strip()
    if not recorded:
        return False
    current = _semantic_rejection_guard_key(cw, mission_epoch, task_id, task)
    return bool(current and current == recorded)


def _record_semantic_rejection_guard(
    agent: Any,
    cw: Any,
    mission_epoch: Any,
    task_id: str,
    result: Mapping[str, Any],
) -> None:
    review = _mapping(result.get("semantic_review"))
    if review.get("review_error") is True or review.get("parse_error") is True:
        return
    if review.get("accepted") is True:
        return
    try:
        task = cw.load_task(task_id)
    except Exception:
        return
    causal_key = _semantic_rejection_guard_key(cw, mission_epoch, task_id, task)
    if not causal_key:
        return
    payload = {
        "schema": "nexus_coding_semantic_rejection_guard.v1",
        "causal_key": causal_key,
        "recorded_at": max(
            (
                _float(event.get("ts"))
                for event in task.get("agent_events") or []
                if isinstance(event, Mapping)
                and str(event.get("type") or "") == _SEMANTIC_STATE_EVENT
            ),
            default=0.0,
        ),
        "reason": str(review.get("reason") or result.get("summary") or "")[:2000],
    }
    try:
        agent._mutate_task(task_id, {_REJECTION_GUARD_KEY: payload})
    except Exception:
        return


def _terminal_state(
    cw: Any,
    mission_epoch: Any,
    task: Mapping[str, Any],
) -> Dict[str, Any]:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return {}

    epoch_key = getattr(mission_epoch, "KEY", "coding_mission_acceptance_epoch")
    epoch = _mapping(task.get(epoch_key))
    if str(epoch.get("status") or "") in {"semantic_accepted", "finalized"}:
        return {}

    threshold = _readiness_threshold(task, mission_epoch)
    if threshold <= 0:
        return {}

    validation_ready, validation_at = _validation_ready(task, threshold)
    if not validation_ready:
        return {}
    review_at = _latest_diff_review_at(task, threshold)
    if not review_at:
        return {}

    if _latest_decisive_rejection(task, threshold):
        # A real semantic rejection deliberately reopens execution so the agent
        # can materially change the diff/hypothesis/evidence. Accepted-but-not-
        # durable and reviewer/protocol failures remain finish-only and retry.
        return {}

    if _semantic_rejection_guard_blocks(cw, mission_epoch, task_id, task):
        # The durable rejection guard outlives bounded agent_events. Without
        # this check, a trimmed rejection event would force finish-only while
        # the run-tool guard deterministically blocks coding_finish — an
        # unrecoverable livelock. Returning empty reopens repair instead.
        return {}

    try:
        delta = mission_epoch.mission_delta_state(cw, task_id, dict(task))
    except Exception:
        return {}
    if not delta.get("ok") or not delta.get("has_delta"):
        return {}

    state_key = hashlib.sha256(
        (
            f"{task_id}|{threshold:.6f}|{validation_at:.6f}|{review_at:.6f}|"
            f"{delta.get('diff_sha256') or ''}"
        ).encode("utf-8")
    ).hexdigest()
    required = (
        "The mission delta has successful post-edit validation and diff review. "
        "Call coding_finish now so the independent semantic acceptance reviewer can "
        "accept or reject the complete mission delta before any further inspection."
    )
    return {
        "schema": _TERMINAL_SCHEMA,
        "status": "active",
        "state_key": state_key,
        "action_kind": _TERMINAL_ACTION,
        "canonical_action_kind": _TERMINAL_ACTION,
        "required_action": required,
        "canonical_required_action": required,
        "allowed_tools": ["coding_finish"],
        "rejection_limit": 2,
        "attempt_count": 0,
        "attempt_limit": 0,
        "stage": "terminal_acceptance",
        "terminal_acceptance_pending": True,
        "mission_diff_sha256": str(delta.get("diff_sha256") or ""),
    }


def _install_terminal_policy(agent: Any, cw: Any, mission_epoch: Any) -> None:
    policy = getattr(agent, "forced_action", None)
    if policy is None or bool(getattr(policy, "_coding_terminal_convergence_installed", False)):
        return
    prior_active = getattr(policy, "active_state", None)
    if not callable(prior_active):
        return

    def active_state_with_terminal_acceptance(task: Mapping[str, Any]) -> Dict[str, Any]:
        state = dict(prior_active(task) or {})
        if state:
            return state
        return _terminal_state(cw, mission_epoch, task)

    policy.active_state = active_state_with_terminal_acceptance
    policy._coding_active_state_before_terminal_convergence = prior_active

    prior_prompt = getattr(policy, "prompt_context", None)
    if callable(prior_prompt):

        def prompt_context_with_terminal_acceptance(task: Mapping[str, Any]) -> str:
            state = policy.active_state(task)
            if str(state.get("schema") or "") == _TERMINAL_SCHEMA:
                return (
                    "Controller terminal-acceptance mode is ACTIVE. Validation and diff review "
                    "already cover the latest mission mutation. Do not inspect or revise anything "
                    "before independent acceptance. Call coding_finish now. If semantic acceptance "
                    "rejects the mission delta, the next turn will reopen execution for repair."
                )
            return str(prior_prompt(task) or "")

        policy.prompt_context = prompt_context_with_terminal_acceptance
        policy._coding_prompt_before_terminal_convergence = prior_prompt

    policy._coding_terminal_convergence_installed = True


def _run_live_refutation(
    agent: Any,
    cw: Any,
    mission_epoch: Any,
    task_id: str,
    args: Mapping[str, Any],
) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    policy = getattr(agent, "forced_action", None)
    state = policy.active_state(task) if callable(getattr(policy, "active_state", None)) else {}
    allowed_names = getattr(policy, "allowed_tool_names", None)
    if callable(allowed_names):
        try:
            allowed = {str(item).strip() for item in allowed_names(task) if str(item).strip()}
        except Exception:
            allowed = set()
    else:
        allowed = {
            str(item).strip()
            for item in (state.get("allowed_tools") or [])
            if str(item).strip()
        }
    tool_name = str(getattr(mission_epoch, "REFUTATION_TOOL", "coding_refute_hypothesis"))
    if str(state.get("action_kind") or "") != "edit" or tool_name not in allowed:
        return {
            "ok": False,
            "error": "hypothesis_refutation_not_available",
            "summary": "coding_refute_hypothesis is available only while the canonical live policy authorizes forced edit mode.",
        }

    reason = str(args.get("reason") or "").strip()
    if len(reason) < 8:
        return {
            "ok": False,
            "error": "hypothesis_refutation_reason_required",
            "summary": "Provide a concrete reason grounded in verified repository evidence.",
        }

    key = str(getattr(mission_epoch, "REFUTATION_KEY", "coding_hypothesis_refutation"))
    schema = str(getattr(mission_epoch, "REFUTATION_SCHEMA", "nexus_coding_hypothesis_refutation.v1"))
    previous = _mapping(task.get(key))
    count = _int(previous.get("count")) if (
        str(previous.get("schema") or "") == schema
        and str(previous.get("status") or "") == "active"
    ) else 0
    if count >= 2:
        return {
            "ok": False,
            "error": "hypothesis_refutation_limit",
            "required_action": "Make the evidence-backed edit or finish with a concrete blocker.",
            "summary": "The bounded hypothesis-refutation escape has already been used twice without a consuming mutation.",
        }

    record = getattr(mission_epoch, "_record_refutation", None)
    if not callable(record):
        return {
            "ok": False,
            "error": "hypothesis_refutation_controller_unavailable",
            "summary": "The mission refutation controller is unavailable; Nexus refused to pretend the hypothesis was refuted.",
        }
    refutation = record(
        agent,
        cw,
        task_id,
        reason=reason,
        contradicting_evidence=str(args.get("contradicting_evidence") or ""),
        forced_state=state,
    )
    return {
        "ok": True,
        "refuted": True,
        "refutation_count": _int(refutation.get("count")),
        "required_action": (
            "Gather at most two targeted repository evidence actions, then record a fresh "
            "four-field remediation hypothesis with coding_update_plan."
        ),
        "summary": (
            "The current remediation hypothesis was explicitly refuted under the live effective "
            "edit policy; forced edit mode is suspended for one bounded evidence pass."
        ),
    }


def _install_live_refutation(agent: Any, guarded: Any, cw: Any, mission_epoch: Any) -> None:
    if bool(getattr(agent, "_coding_live_refutation_execution_installed", False)):
        return
    prior_run_tool = agent._run_tool
    refutation_tool = str(getattr(mission_epoch, "REFUTATION_TOOL", "coding_refute_hypothesis"))

    def run_tool_with_live_refutation(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        normalized_name = str(name or "")
        if normalized_name == refutation_tool:
            return _run_live_refutation(
                agent,
                cw,
                mission_epoch,
                task_id,
                args,
            )
        if normalized_name == "coding_finish":
            try:
                before = cw.load_task(task_id)
            except Exception:
                before = {}
            if before and _semantic_rejection_guard_blocks(
                cw,
                mission_epoch,
                task_id,
                before,
            ):
                return {
                    "ok": False,
                    "success": False,
                    "error": "semantic_acceptance_state_unchanged",
                    "required_action": (
                        "Change the repository diff or materially revise the remediation hypothesis/evidence, "
                        "then rerun validation and diff review before finishing again."
                    ),
                    "summary": (
                        "Independent semantic acceptance already rejected this causal repository state. "
                        "Project-plan status or bookkeeping changes do not make the same patch eligible for another review."
                    ),
                }
        result = prior_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )
        if (
            normalized_name == "coding_finish"
            and str(result.get("error") or "") == "semantic_acceptance_rejected"
        ):
            _record_semantic_rejection_guard(
                agent,
                cw,
                mission_epoch,
                task_id,
                result,
            )
        return result

    agent._run_tool = run_tool_with_live_refutation
    guarded._run_tool_with_semantic_acceptance = run_tool_with_live_refutation
    agent._coding_run_tool_before_live_refutation = prior_run_tool
    agent._coding_live_refutation_execution_installed = True


def _install_consumed_lifecycle_continuity() -> None:
    """Keep consumed causal evidence available even if the plan later changes."""
    from app import coding_completion_state_hardening as completion

    if bool(getattr(completion, "_coding_consumed_lifecycle_continuity_installed", False)):
        return

    prior_record = completion._record_consumed_hypothesis

    def record_consumed_hypothesis_with_note(
        agent: Any,
        cw: Any,
        persistence: Any,
        *,
        task_id: str,
        before_task: Mapping[str, Any],
        before_state: Mapping[str, Any],
        tool_name: str,
    ) -> None:
        prior_record(
            agent,
            cw,
            persistence,
            task_id=task_id,
            before_task=before_task,
            before_state=before_state,
            tool_name=tool_name,
        )
        note = str(_mapping(before_task.get("project_plan")).get("note") or "").strip()
        if not note:
            return
        try:
            latest = cw.load_task(task_id)
        except Exception:
            return
        lifecycle = dict(_mapping(latest.get(_LIFECYCLE_KEY)))
        if str(lifecycle.get("status") or "") != "consumed":
            return
        lifecycle["consumed_hypothesis_note"] = note
        structured = _structured_hypothesis_fingerprint_from_note(note)
        if structured:
            lifecycle["structured_hypothesis_fingerprint"] = structured
        try:
            agent._mutate_task(task_id, {_LIFECYCLE_KEY: lifecycle})
        except Exception:
            return

    completion._record_consumed_hypothesis = record_consumed_hypothesis_with_note
    completion._record_consumed_hypothesis_before_convergence = prior_record

    prior_context = completion._lifecycle_context

    def lifecycle_context_with_durable_consumed_evidence(task: Mapping[str, Any]) -> str:
        lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
        if str(lifecycle.get("status") or "") != "consumed":
            return prior_context(task)
        evidence = str(lifecycle.get("verified_evidence_digest") or "").strip()
        consumed_note = str(lifecycle.get("consumed_hypothesis_note") or "").strip()
        if not consumed_note:
            current_note = str(_mapping(task.get("project_plan")).get("note") or "").strip()
            expected = str(lifecycle.get("note_fingerprint") or "").strip()
            if current_note and expected and _note_fingerprint(current_note) == expected:
                consumed_note = current_note
        bits = [
            "Hypothesis lifecycle: consumed by a repository mutation.",
            "The remediation hypothesis that justified the latest mutation is historical audit context, not current causal truth.",
            "Later project-plan bookkeeping does not erase the consumed hypothesis or its verified pre-edit evidence.",
            "Revalidate repository evidence before using a new hypothesis to justify another edit or terminal claim.",
        ]
        if consumed_note:
            bits.extend(["", "Consumed remediation hypothesis:", consumed_note])
        if evidence:
            bits.extend(["", "Verified pre-edit repository evidence snapshot:", evidence])
        return "\n".join(bits)

    completion._lifecycle_context = lifecycle_context_with_durable_consumed_evidence
    completion._lifecycle_context_before_convergence = prior_context
    completion._coding_consumed_lifecycle_continuity_installed = True


def _install_validation_continuity() -> None:
    """Persist validation obligations across checkpoints and runner attempts.

    Replaces the terminal layer's provenance writer with one atomic
    implementation that (a) refuses non-validation commands — so a failed
    ``cat missing-file`` can never become an unclearable validation obligation
    — and (b) writes the durable record and its bounded history in a single
    mutation, so concurrent validations cannot drop each other's records.
    """
    from app import coding_terminal_acceptance_hardening as terminal

    if bool(getattr(terminal, "_coding_validation_history_continuity_installed", False)):
        return
    prior_persist = terminal._persist_validation_provenance

    def persist_validation_atomically(
        cw: Any,
        work_phases: Any,
        *,
        task_id: str,
        argv: Any,
        cwd: Any,
        result: Mapping[str, Any],
    ) -> None:
        try:
            qualifies = bool(work_phases.is_validation_command(argv))
        except Exception:
            qualifies = False
        if not qualifies:
            return
        signature = _validation_signature(argv)
        if not signature:
            return
        try:
            before = cw.load_task(task_id)
        except Exception:
            return
        latest_command_timestamp = getattr(terminal, "_latest_command_timestamp", None)
        ts = 0.0
        if callable(latest_command_timestamp):
            try:
                ts = _float(latest_command_timestamp(before, argv=list(signature)))
            except Exception:
                ts = 0.0
        ts = ts or time.time()
        substantive = not _validation_result_missing_tool(result)
        record = {
            "argv": list(signature),
            "ok": result.get("ok") is True,
            "ts": ts,
            "cwd": str(cwd or ""),
            "substantive": substantive,
        }

        def apply(task: Dict[str, Any]) -> None:
            current = dict(_mapping(task.get(_VALIDATION_KEY)))
            history = [
                dict(item)
                for item in (current.get("history") or [])
                if isinstance(item, Mapping)
            ]
            duplicate = any(
                _validation_signature(item.get("argv")) == signature
                and abs(_float(item.get("ts")) - ts) < 1e-6
                and bool(item.get("ok")) == bool(record["ok"])
                for item in history
            )
            if not duplicate:
                history.append(dict(record))
            task[_VALIDATION_KEY] = {
                "schema": "nexus_coding_validation_provenance.v1",
                "argv": list(signature),
                "ok": result.get("ok") is True,
                "ts": ts,
                "cwd": str(cwd or ""),
                "run_id": str(task.get("agent_run_id") or ""),
                "cycle": int(task.get("agent_cycle") or 0),
                "substantive": substantive,
                "history": history[-_VALIDATION_HISTORY_LIMIT:],
            }

        mutate = getattr(cw, "mutate_task", None)
        if callable(mutate):
            try:
                mutate(task_id, apply)
                return
            except Exception:
                pass
        try:
            fallback = cw.load_task(task_id)
            apply(fallback)
            cw.save_task(fallback)
        except Exception:
            return

    terminal._persist_validation_provenance = persist_validation_atomically
    terminal._persist_validation_provenance_before_convergence = prior_persist
    terminal._coding_validation_history_continuity_installed = True


def _install_retryable_acceptance_failures() -> None:
    """Treat reviewer transport/protocol failures as retryable, not semantic truth."""
    from app import coding_terminal_acceptance_hardening as terminal

    if bool(getattr(terminal, "_coding_retryable_acceptance_failures_installed", False)):
        return
    prior_record = terminal._record_rejection

    def record_rejection_with_retryable_parse_failure(
        agent: Any,
        task_id: str,
        task: Mapping[str, Any],
        *,
        fingerprint: str,
        result: Mapping[str, Any],
    ) -> None:
        adjusted = dict(result)
        review = dict(_mapping(adjusted.get("semantic_review")))
        if review.get("parse_error") is True:
            review["review_error"] = True
            adjusted["semantic_review"] = review
        prior_record(
            agent,
            task_id,
            task,
            fingerprint=fingerprint,
            result=adjusted,
        )

    terminal._record_rejection = record_rejection_with_retryable_parse_failure
    terminal._record_rejection_before_convergence = prior_record
    terminal._coding_retryable_acceptance_failures_installed = True


def _install_semantic_evidence_instruction(semantic_acceptance: Any) -> None:
    if bool(getattr(semantic_acceptance, "_coding_acceptance_evidence_instruction_installed", False)):
        return
    prior_build = semantic_acceptance.build_review_messages

    def build_review_messages(**kwargs: Any) -> tuple[str, str]:
        system, user = prior_build(**kwargs)
        system += (
            " Treat any verified repository evidence embedded in the hypothesis lifecycle as "
            "authoritative evidence rather than author narrative. Reject causal_alignment when "
            "that evidence exposes an alternative causal path the patch leaves untouched. Reject "
            "root-cause claims about runtime, deployment, environment-variable, or configuration "
            "state unless the supplied evidence actually establishes that state; a code branch that "
            "would execute if a variable were unset is not evidence that the variable is unset."
        )
        return system, user

    semantic_acceptance.build_review_messages = build_review_messages
    semantic_acceptance._build_review_messages_before_acceptance_evidence_instruction = prior_build
    semantic_acceptance._coding_acceptance_evidence_instruction_installed = True


def install(
    agent: Any,
    guarded: Any,
    cw: Any,
    mission_epoch: Any,
    semantic_acceptance: Any,
) -> None:
    """Make refutation execution and terminal mission acceptance converge on live policy."""
    _install_consumed_lifecycle_continuity()
    _install_validation_continuity()
    _install_retryable_acceptance_failures()
    _install_live_refutation(agent, guarded, cw, mission_epoch)
    _install_terminal_policy(agent, cw, mission_epoch)
    _install_semantic_evidence_instruction(semantic_acceptance)
