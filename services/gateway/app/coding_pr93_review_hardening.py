from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, Mapping


log = logging.getLogger(__name__)

_VALIDATION_KEY = "coding_validation_provenance"
_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
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


def _signature(argv: Any) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in argv if str(item).strip())


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


def structured_hypothesis_fields(note: Any) -> Dict[str, str]:
    """Parse only the four causal fields without truncating locator-like lines.

    Unknown ``label:`` lines inside a field remain value content. After all four
    known fields have been found, a clearly bookkeeping-like suffix may be
    stripped from the final field. This preserves multiline repository evidence
    such as ``app.py: ...`` while keeping status/state sections non-causal.
    """
    text = str(note or "").strip()
    if not text:
        return {}
    matches = list(_HYPOTHESIS_FIELD_RE.finditer(text))
    if not matches:
        return {}
    fields: Dict[str, str] = {}
    seen_matches: list[tuple[str, re.Match[str]]] = []
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
        if label and label not in fields:
            seen_matches.append((label, match))
            fields[label] = ""
    if [label for label, _match in seen_matches] != list(_HYPOTHESIS_FIELDS):
        return {}

    parsed: Dict[str, str] = {}
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
        parsed[label] = value
    return parsed


def structured_hypothesis_fingerprint_from_note(note: Any) -> str:
    fields = structured_hypothesis_fields(note)
    if not fields:
        return ""
    payload = "v2\x1f" + "\x1f".join(
        f"{label}\x1e{fields[label]}" for label in _HYPOTHESIS_FIELDS
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _structured_hypothesis_fingerprint(task: Mapping[str, Any]) -> str:
    plan = _mapping(task.get("project_plan"))
    return structured_hypothesis_fingerprint_from_note(plan.get("note"))


def _material_hypothesis_updated_at(task: Mapping[str, Any]) -> float:
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    if str(lifecycle.get("status") or "") != "consumed":
        return 0.0
    plan = _mapping(task.get("project_plan"))
    current = _structured_hypothesis_fingerprint(task)
    if not current:
        return 0.0

    consumed_candidates = {
        str(lifecycle.get("structured_hypothesis_fingerprint_v2") or "").strip(),
        str(lifecycle.get("structured_hypothesis_fingerprint") or "").strip(),
    }
    consumed_note = str(lifecycle.get("consumed_hypothesis_note") or "").strip()
    if consumed_note:
        consumed_candidates.add(structured_hypothesis_fingerprint_from_note(consumed_note))
    consumed_candidates.discard("")
    if current in consumed_candidates:
        return 0.0

    # Legacy lifecycle records may not carry the consumed note. Preserve the
    # pre-v2 exact-note fallback rather than manufacturing a causal change.
    if not consumed_candidates:
        consumed_note_fp = str(lifecycle.get("note_fingerprint") or "").strip()
        normalized = str(plan.get("note") or "").strip()
        current_note_fp = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if not consumed_note_fp or current_note_fp == consumed_note_fp:
            return 0.0

    if _int(plan.get("revision")) <= _int(lifecycle.get("plan_revision")):
        return 0.0
    return _float(plan.get("updated_at"))


def validation_records_from_events(
    convergence: Any,
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
            args = _mapping(event.get("args"))
            argv = args.get("argv")
            signature = _signature(argv) if coding_work_phases.is_validation_command(argv) else None
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
        if convergence._validation_result_missing_tool(result):
            continue
        finished_at = _float(event.get("ts")) or started_at
        if finished_at >= threshold:
            output.append((finished_at, signature, result.get("ok") is True))
    return output


def validation_records(
    convergence: Any,
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...], bool]]:
    records = [
        *convergence._validation_records_from_history(task, threshold),
        *convergence._validation_records_from_events(task, threshold),
        *convergence._validation_records_from_commands(task, threshold),
    ]
    durable = _mapping(task.get(_VALIDATION_KEY))
    durable_ts = _float(durable.get("ts"))
    durable_signature = _signature(durable.get("argv"))
    durable_substantive = durable.get("substantive") is not False
    if durable_ts >= threshold and durable_signature and durable_substantive:
        record = (durable_ts, durable_signature, durable.get("ok") is True)
        if record not in records:
            records.append(record)
    unique = {record: record for record in records}
    return sorted(unique.values(), key=lambda item: item[0])


def unresolved_validation_failures(
    convergence: Any,
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...]]]:
    records = validation_records(convergence, task, threshold)
    failures: list[tuple[float, tuple[str, ...]]] = []
    for failed_at, failed_signature, ok in records:
        if ok:
            continue
        if any(
            later_at > failed_at and later_signature == failed_signature and later_ok
            for later_at, later_signature, later_ok in records
        ):
            continue
        failures.append((failed_at, failed_signature))
    return failures


def validation_obligations_ready(
    convergence: Any,
    task: Mapping[str, Any],
    threshold: float,
) -> bool:
    return not unresolved_validation_failures(convergence, task, threshold)


def _install_atomic_validation_persistence(convergence: Any) -> None:
    from app import coding_terminal_acceptance_hardening as terminal

    if bool(getattr(terminal, "_coding_pr93_atomic_validation_persistence_installed", False)):
        return

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
        signature = _signature(argv) if qualifies else ()
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
        substantive = not convergence._validation_result_missing_tool(result)
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
                _signature(item.get("argv")) == signature
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
                "history": history[-32:],
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
    terminal._coding_pr93_atomic_validation_persistence_installed = True
    # Mark both historical installers satisfied. Acceptance and resume may now
    # run in either order without wrapping the fixed implementation again.
    terminal._coding_validation_history_continuity_installed = True
    terminal._coding_pr93_validation_persistence_fixed = True


def _install_rejection_guard(convergence: Any) -> None:
    if bool(getattr(convergence, "_coding_pr93_terminal_rejection_guard_installed", False)):
        return
    prior_terminal_state = convergence._terminal_state

    def terminal_state_with_rejection_guard(
        next_cw: Any,
        next_mission_epoch: Any,
        task: Mapping[str, Any],
    ) -> Dict[str, Any]:
        task_id = str(task.get("id") or "").strip()
        if task_id and convergence._semantic_rejection_guard_blocks(
            next_cw, next_mission_epoch, task_id, task
        ):
            return {}
        return dict(prior_terminal_state(next_cw, next_mission_epoch, task) or {})

    convergence._terminal_state = terminal_state_with_rejection_guard
    convergence._coding_pr93_terminal_rejection_guard_installed = True


def _install_safe_sentinel_guard(resume: Any) -> None:
    def install_sentinel_failed_resume_guard() -> None:
        try:
            from app import sentinel_runtime
        except Exception:
            # If Sentinel itself is unavailable there is no auto-resume loop to
            # constrain. Do not take the Coding API down with supervision import.
            log.exception("Sentinel unavailable while installing coding auto-resume blockers")
            return
        raw = getattr(sentinel_runtime, "_CODING_AUTO_RESUME_BLOCKERS", ())
        try:
            blockers = set(raw or ())
        except Exception:
            blockers = set()
            log.error("Sentinel auto-resume blocker collection drifted; replacing it fail-closed")
        blockers.add("run_failed")
        sentinel_runtime._CODING_AUTO_RESUME_BLOCKERS = blockers

    resume._install_sentinel_failed_resume_guard = install_sentinel_failed_resume_guard


def _install_resume_bridges(resume: Any, convergence: Any, mission_epoch: Any) -> None:
    def install_convergence_review_fixes(_cw: Any, _mission_epoch: Any, _convergence: Any) -> None:
        _install_rejection_guard(convergence)

    resume._install_convergence_review_fixes = install_convergence_review_fixes
    resume._install_validation_persistence_fix = lambda _convergence: _install_atomic_validation_persistence(convergence)
    resume._install_validation_side_effect_restamp = lambda *_args, **_kwargs: None
    resume._restamp_validation_after_workspace_mutation = lambda *_args, **_kwargs: None
    resume._unresolved_validation_failures = (
        lambda next_convergence, task, threshold: unresolved_validation_failures(
            next_convergence, task, threshold
        )
    )

    if not bool(getattr(resume, "_coding_pr93_explicit_repair_refutation_installed", False)):
        prior_post_edit_state = resume.post_edit_state

        def post_edit_state_with_explicit_refutation(
            cw: Any,
            next_mission_epoch: Any,
            next_convergence: Any,
            task: Mapping[str, Any],
        ) -> Dict[str, Any]:
            state = dict(
                prior_post_edit_state(cw, next_mission_epoch, next_convergence, task) or {}
            )
            if state.get("validation_repair") is not True:
                return state
            refutation_tool = str(
                getattr(next_mission_epoch, "REFUTATION_TOOL", "coding_refute_hypothesis")
            )
            state["allowed_tools"] = sorted(
                set(state.get("allowed_tools") or []) | {refutation_tool}
            )
            state["required_action"] = (
                str(state.get("required_action") or "").rstrip()
                + " If the failing validation contradicts the consumed causal hypothesis, "
                + f"{refutation_tool} is explicitly available."
            )
            state["canonical_required_action"] = state["required_action"]
            return state

        resume.post_edit_state = post_edit_state_with_explicit_refutation
        resume._coding_pr93_explicit_repair_refutation_installed = True


def preinstall(
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
    resume: Any,
) -> None:
    """Install order-independent PR #93 review fixes before either overlay runs."""
    convergence._structured_hypothesis_fields = structured_hypothesis_fields
    convergence._structured_hypothesis_fingerprint_from_note = structured_hypothesis_fingerprint_from_note
    convergence._structured_hypothesis_fingerprint = _structured_hypothesis_fingerprint
    convergence._material_hypothesis_updated_at = _material_hypothesis_updated_at
    convergence._validation_records_from_events = (
        lambda task, threshold: validation_records_from_events(convergence, task, threshold)
    )
    convergence._validation_records = (
        lambda task, threshold: validation_records(convergence, task, threshold)
    )
    convergence._unresolved_validation_failures = (
        lambda task, threshold: unresolved_validation_failures(convergence, task, threshold)
    )
    convergence._validation_obligations_ready = (
        lambda task, threshold: validation_obligations_ready(convergence, task, threshold)
    )

    def install_validation_continuity_safely() -> None:
        _install_atomic_validation_persistence(convergence)

    # Acceptance.install() resolves this name from the module at call time. By
    # replacing the installer itself before invocation, reversed install order,
    # partial install, and later repeated install cannot resurrect the polluting
    # wrapper from the earlier implementation.
    convergence._install_validation_continuity = install_validation_continuity_safely
    _install_atomic_validation_persistence(convergence)
    _install_safe_sentinel_guard(resume)
    _install_resume_bridges(resume, convergence, mission_epoch)
