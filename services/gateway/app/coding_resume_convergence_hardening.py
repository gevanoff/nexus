from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, Mapping


SCHEMA = "nexus_coding_resume_convergence.v1"
_SENTINEL_FAILED_ATTENTION = "run_failed"
_VALIDATION_KEY = "coding_validation_provenance"
_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
_HYPOTHESIS_FIELDS = (
    "Root cause",
    "Repository evidence",
    "Competing explanation checked",
    "Expected result",
)
_ANY_PLAN_FIELD_RE = re.compile(r"(?im)(?:^|[\n;])\s*([^:\n;]{1,120})\s*:\s*")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _signature(argv: Any) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in argv if str(item).strip())


def _active_refutation(task: Mapping[str, Any], mission_epoch: Any) -> bool:
    key = str(getattr(mission_epoch, "REFUTATION_KEY", "coding_hypothesis_refutation"))
    schema = str(getattr(mission_epoch, "REFUTATION_SCHEMA", "nexus_coding_hypothesis_refutation.v1"))
    refutation = _mapping(task.get(key))
    return bool(
        str(refutation.get("schema") or "") == schema
        and str(refutation.get("status") or "") == "active"
    )


def _pending_replacement_hypothesis(
    convergence: Any,
    mission_epoch: Any,
    task: Mapping[str, Any],
) -> bool:
    """Return true when a replacement hypothesis still needs a consuming edit."""
    material_update = _float(convergence._material_hypothesis_updated_at(task))
    if material_update <= 0:
        return False
    epoch_key = str(getattr(mission_epoch, "KEY", "coding_mission_acceptance_epoch"))
    epoch = _mapping(task.get(epoch_key))
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    latest_consuming_mutation = max(
        _float(epoch.get("last_mutation_at")),
        _float(lifecycle.get("consumed_at")),
    )
    return material_update > latest_consuming_mutation


def _state(
    *,
    task_id: str,
    action_kind: str,
    required_action: str,
    allowed_tools: list[str],
    threshold: float,
    diff_sha256: str,
    validation_at: float = 0.0,
    review_at: float = 0.0,
    stage: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    state_key = hashlib.sha256(
        (
            f"{task_id}|{action_kind}|{threshold:.6f}|{validation_at:.6f}|"
            f"{review_at:.6f}|{diff_sha256}|{stage or ''}"
        ).encode("utf-8")
    ).hexdigest()
    state = {
        "schema": SCHEMA,
        "status": "active",
        "state_key": state_key,
        "action_kind": action_kind,
        "canonical_action_kind": action_kind,
        "required_action": required_action,
        "canonical_required_action": required_action,
        "allowed_tools": sorted(set(allowed_tools)),
        "rejection_limit": 2,
        "attempt_count": 0,
        "attempt_limit": 0,
        "stage": stage or f"post_edit_{action_kind}",
        "mission_acceptance_pending": True,
        "mission_diff_sha256": diff_sha256,
    }
    if extra:
        state.update(dict(extra))
    return state


def _validation_records(
    convergence: Any,
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...], bool]]:
    records = [
        *convergence._validation_records_from_history(task, threshold),
        *convergence._validation_records_from_events(task, threshold),
        *convergence._validation_records_from_commands(task, threshold),
    ]
    validation = _mapping(task.get(_VALIDATION_KEY))
    durable_ts = _float(validation.get("ts"))
    durable_signature = _signature(validation.get("argv"))
    if durable_ts >= threshold and durable_signature:
        represented = any(
            abs(ts - durable_ts) < 1e-6 and signature == durable_signature
            for ts, signature, _ok in records
        )
        if not represented:
            records.append((durable_ts, durable_signature, validation.get("ok") is True))
    unique: dict[tuple[float, tuple[str, ...], bool], tuple[float, tuple[str, ...], bool]] = {}
    for record in records:
        unique[record] = record
    return sorted(unique.values(), key=lambda item: item[0])


def _unresolved_validation_failures(
    convergence: Any,
    task: Mapping[str, Any],
    threshold: float,
) -> list[tuple[float, tuple[str, ...]]]:
    records = _validation_records(convergence, task, threshold)
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


def post_edit_state(
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
    task: Mapping[str, Any],
) -> Dict[str, Any]:
    """Derive mission-scoped post-edit convergence across runner attempts."""
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return {}

    epoch_key = str(getattr(mission_epoch, "KEY", "coding_mission_acceptance_epoch"))
    epoch = _mapping(task.get(epoch_key))
    if str(epoch.get("status") or "") != "pending":
        return {}

    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    mutation_at = max(
        _float(epoch.get("last_mutation_at")),
        _float(lifecycle.get("consumed_at")),
    )
    if mutation_at <= 0:
        return {}

    # An explicit refutation or a replacement hypothesis is a repair path, not
    # a validation phase for the previously rejected patch.
    if _active_refutation(task, mission_epoch):
        return {}
    if _pending_replacement_hypothesis(convergence, mission_epoch, task):
        return {}

    threshold = _float(convergence._readiness_threshold(task, mission_epoch))
    if threshold <= 0:
        return {}
    if convergence._latest_decisive_rejection(task, threshold):
        return {}
    if convergence._semantic_rejection_guard_blocks(
        cw,
        mission_epoch,
        task_id,
        task,
    ):
        return {}

    try:
        delta = mission_epoch.mission_delta_state(cw, task_id, dict(task))
    except Exception:
        return {}
    if not delta.get("ok") or not delta.get("has_delta"):
        return {}
    diff_sha = str(delta.get("diff_sha256") or "")

    validation_ready, validation_at = convergence._validation_ready(task, threshold)
    if not validation_ready:
        unresolved = _unresolved_validation_failures(convergence, task, threshold)
        if unresolved:
            labels = [" ".join(signature) for _ts, signature in unresolved[-3:]]
            failing = "; ".join(labels)
            required = (
                "Post-edit validation failed and the failure is still unresolved. Repair the "
                "smallest evidence-backed defect with structured edit tools, materially revise "
                "the plan if the causal hypothesis changed, or rerun the same failing validation "
                f"after an environmental correction. Unresolved validation: {failing}. Do not "
                "substitute a weaker green check for the failing signature."
            )
            return _state(
                task_id=task_id,
                action_kind="edit",
                required_action=required,
                allowed_tools=[
                    "coding_write_file",
                    "coding_replace_text",
                    "coding_apply_patch",
                    "coding_run_command",
                    "coding_update_plan",
                    "coding_finish",
                ],
                threshold=threshold,
                diff_sha256=diff_sha,
                validation_at=validation_at,
                stage="post_edit_validation_repair",
                extra={
                    "validation_repair": True,
                    "unresolved_validation_signatures": labels,
                },
            )

        required = (
            "The pending mission delta has not passed validation after its latest mutation. "
            "Run one targeted validation command now. Do not inspect, edit, revise the plan, "
            "or review the diff first. If validation cannot be run, call coding_finish with "
            "success=false and a concrete blocker."
        )
        return _state(
            task_id=task_id,
            action_kind="validate",
            required_action=required,
            allowed_tools=["coding_run_command", "coding_finish"],
            threshold=threshold,
            diff_sha256=diff_sha,
            validation_at=validation_at,
        )

    review_at = convergence._latest_diff_review_at(task, threshold)
    if not review_at:
        required = (
            "The pending mission delta has passed post-mutation validation but has not been "
            "diff-reviewed after its latest mutation. Call coding_git_diff now. Do not reopen "
            "inspection, edit, or plan work before reviewing the diff."
        )
        return _state(
            task_id=task_id,
            action_kind="review",
            required_action=required,
            allowed_tools=["coding_git_diff", "coding_finish"],
            threshold=threshold,
            diff_sha256=diff_sha,
            validation_at=validation_at,
        )

    return dict(convergence._terminal_state(cw, mission_epoch, task) or {})


def _install_policy(
    agent: Any,
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
) -> None:
    policy = getattr(agent, "forced_action", None)
    if policy is None or bool(getattr(policy, "_coding_resume_convergence_installed", False)):
        return
    prior_active = getattr(policy, "active_state", None)
    if not callable(prior_active):
        return

    def active_state_with_resume_convergence(task: Mapping[str, Any]) -> Dict[str, Any]:
        derived = post_edit_state(cw, mission_epoch, convergence, task)
        if derived:
            return derived
        return dict(prior_active(task) or {})

    policy.active_state = active_state_with_resume_convergence
    policy._coding_active_state_before_resume_convergence = prior_active

    prior_prompt = getattr(policy, "prompt_context", None)
    if callable(prior_prompt):

        def prompt_context_with_resume_convergence(task: Mapping[str, Any]) -> str:
            state = policy.active_state(task)
            if str(state.get("schema") or "") == SCHEMA:
                if state.get("validation_repair") is True:
                    failing = "; ".join(state.get("unresolved_validation_signatures") or [])
                    return (
                        "Controller post-edit validation-repair mode is ACTIVE. A substantive "
                        f"validation failure remains unresolved: {failing}. Repair it with the "
                        "smallest structured edit, revise the plan only if the causal hypothesis "
                        "changed, or rerun that same validation after an environmental correction. "
                        "coding_run_command remains validation-only; shell edits are not authorized."
                    )
                kind = str(state.get("action_kind") or "")
                if kind == "validate":
                    return (
                        "Controller post-edit convergence is ACTIVE. The complete pending mission "
                        "delta needs validation after its latest mutation. Call coding_run_command "
                        "with one targeted validation now; do not inspect, edit, review, or revise "
                        "the plan first."
                    )
                if kind == "review":
                    return (
                        "Controller post-edit convergence is ACTIVE. Validation is current and the "
                        "complete pending mission delta now needs diff review. Call coding_git_diff "
                        "now; do not inspect, edit, or revise the plan first."
                    )
            return str(prior_prompt(task) or "")

        policy.prompt_context = prompt_context_with_resume_convergence
        policy._coding_prompt_before_resume_convergence = prior_prompt

    policy._coding_resume_convergence_installed = True


def _fixed_structured_hypothesis_fields(note: Any) -> Dict[str, str]:
    """Parse the four causal fields while ignoring arbitrary trailing plan keys."""
    text = str(note or "").strip()
    if not text:
        return {}
    matches = list(_ANY_PLAN_FIELD_RE.finditer(text))
    if not matches:
        return {}
    fields: Dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = str(match.group(1) or "").strip()
        label = next(
            (candidate for candidate in _HYPOTHESIS_FIELDS if candidate.casefold() == raw_label.casefold()),
            "",
        )
        if not label or label in fields:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip(" \t\r\n;")
        if not value:
            return {}
        fields[label] = value
    if not all(label in fields for label in _HYPOTHESIS_FIELDS):
        return {}
    return fields


def _install_convergence_review_fixes(
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
) -> None:
    if bool(getattr(convergence, "_coding_pr93_review_fixes_installed", False)):
        return

    # Keep causal identity stable across terse fields and arbitrary bookkeeping
    # sections such as "Status (auto): ..." or non-ASCII labels.
    convergence._structured_hypothesis_fields = _fixed_structured_hypothesis_fields

    # Pair command events by call id exactly. FIFO pairing is only valid for
    # legacy id-less starts/finishes, and non-validation starts must still
    # consume their id-less finish slot so they cannot steal validation state.
    def validation_records_from_events(
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
            if call_id:
                started = pending_by_id.pop(call_id, None)
            else:
                started = pending_without_id.pop(0) if pending_without_id else None
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
            if finished_at < threshold:
                continue
            output.append((finished_at, signature, result.get("ok") is True))
        return output

    convergence._validation_records_from_events = validation_records_from_events

    # A durable semantic rejection guard must win even if the corresponding
    # bounded event was trimmed or a later mutation timestamp moved the event
    # below the readiness threshold.
    prior_terminal_state = convergence._terminal_state

    def terminal_state_with_rejection_guard(
        next_cw: Any,
        next_mission_epoch: Any,
        task: Mapping[str, Any],
    ) -> Dict[str, Any]:
        task_id = str(task.get("id") or "").strip()
        if task_id and convergence._semantic_rejection_guard_blocks(
            next_cw,
            next_mission_epoch,
            task_id,
            task,
        ):
            return {}
        return dict(prior_terminal_state(next_cw, next_mission_epoch, task) or {})

    convergence._terminal_state = terminal_state_with_rejection_guard
    convergence._terminal_state_before_pr93_review_fix = prior_terminal_state

    # Cosmetic hypothesis rewording must not buy another stochastic review of
    # the identical diff. A same-diff retry requires new verified evidence; a
    # consuming mutation naturally changes the mission diff key.
    def semantic_rejection_guard_key(
        next_cw: Any,
        next_mission_epoch: Any,
        task_id: str,
        task: Mapping[str, Any],
    ) -> str:
        try:
            delta = next_mission_epoch.mission_delta_state(next_cw, task_id, dict(task))
        except Exception:
            return ""
        if not delta.get("ok") or not delta.get("has_delta"):
            return ""
        lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
        payload = "\x1f".join(
            [
                str(delta.get("diff_sha256") or ""),
                str(lifecycle.get("verified_evidence_digest") or ""),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    convergence._semantic_rejection_guard_key = semantic_rejection_guard_key
    convergence._coding_pr93_review_fixes_installed = True


def _install_validation_persistence_fix(convergence: Any) -> None:
    """Persist only real validations and merge history atomically."""
    from app import coding_terminal_acceptance_hardening as terminal

    if bool(getattr(terminal, "_coding_pr93_validation_persistence_fixed", False)):
        return
    base_persist = getattr(
        terminal,
        "_persist_validation_provenance_before_convergence",
        terminal._persist_validation_provenance,
    )

    def persist_validation_with_safe_history(
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

        base_persist(
            cw,
            work_phases,
            task_id=task_id,
            argv=argv,
            cwd=cwd,
            result=result,
        )
        signature = _signature(argv)
        if not signature:
            return
        try:
            latest = cw.load_task(task_id)
        except Exception:
            return
        durable = _mapping(latest.get(_VALIDATION_KEY))
        if _signature(durable.get("argv")) != signature:
            return
        ledger_ts = 0.0
        latest_command_timestamp = getattr(terminal, "_latest_command_timestamp", None)
        if callable(latest_command_timestamp):
            try:
                ledger_ts = _float(latest_command_timestamp(latest, argv=list(signature)))
            except Exception:
                ledger_ts = 0.0
        ts = ledger_ts or _float(durable.get("ts")) or time.time()
        record = {
            "argv": list(signature),
            "ok": result.get("ok") is True,
            "ts": ts,
            "cwd": str(cwd or ""),
            "substantive": not convergence._validation_result_missing_tool(result),
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
            current["history"] = history[-32:]
            task[_VALIDATION_KEY] = current

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

    terminal._persist_validation_provenance = persist_validation_with_safe_history
    terminal._persist_validation_provenance_before_pr93_review_fix = base_persist
    terminal._coding_pr93_validation_persistence_fixed = True


def _restamp_validation_after_workspace_mutation(
    cw: Any,
    mission_epoch: Any,
    task_id: str,
    argv: Any,
) -> None:
    signature = _signature(argv)
    if not signature:
        return
    try:
        task = cw.load_task(task_id)
    except Exception:
        return
    epoch_key = str(getattr(mission_epoch, "KEY", "coding_mission_acceptance_epoch"))
    mutation_at = _float(_mapping(task.get(epoch_key)).get("last_mutation_at"))
    validation = _mapping(task.get(_VALIDATION_KEY))
    if mutation_at <= 0 or _signature(validation.get("argv")) != signature:
        return
    if _float(validation.get("ts")) >= mutation_at:
        return
    stamped_at = max(time.time(), mutation_at + 1e-6)

    def apply(latest: Dict[str, Any]) -> None:
        current = dict(_mapping(latest.get(_VALIDATION_KEY)))
        if _signature(current.get("argv")) != signature:
            return
        old_ts = _float(current.get("ts"))
        current["ts"] = stamped_at
        history = [
            dict(item)
            for item in (current.get("history") or [])
            if isinstance(item, Mapping)
        ]
        for item in reversed(history):
            if _signature(item.get("argv")) != signature:
                continue
            if old_ts and abs(_float(item.get("ts")) - old_ts) > 1e-3:
                continue
            item["ts"] = stamped_at
            break
        current["history"] = history
        latest[_VALIDATION_KEY] = current

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


def _install_validation_side_effect_restamp(
    agent: Any,
    cw: Any,
    mission_epoch: Any,
) -> None:
    if bool(getattr(agent, "_coding_pr93_validation_restamp_installed", False)):
        return
    from app import coding_agent_guarded as guarded
    from app import coding_work_phases

    prior_run_tool = agent._run_tool

    def run_tool_with_validation_restamp(
        task_id: str,
        name: str,
        args: Dict[str, Any],
        *,
        git_token_value: Any,
    ) -> Dict[str, Any]:
        result = prior_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )
        if (
            str(name or "") == "coding_run_command"
            and result.get("workspace_modified") is True
        ):
            argv = args.get("argv")
            try:
                qualifies = bool(coding_work_phases.is_validation_command(argv))
            except Exception:
                qualifies = False
            if qualifies:
                _restamp_validation_after_workspace_mutation(
                    cw,
                    mission_epoch,
                    task_id,
                    argv,
                )
        return result

    agent._run_tool = run_tool_with_validation_restamp
    guarded._run_tool_with_semantic_acceptance = run_tool_with_validation_restamp
    agent._coding_run_tool_before_pr93_validation_restamp = prior_run_tool
    agent._coding_pr93_validation_restamp_installed = True


def _install_sentinel_failed_resume_guard() -> None:
    """Make generic failed runs a fail-closed Sentinel attention state."""
    from app import sentinel_runtime

    blockers = getattr(sentinel_runtime, "_CODING_AUTO_RESUME_BLOCKERS", None)
    if not isinstance(blockers, set):
        raise RuntimeError(
            "Sentinel auto-resume policy drifted: _CODING_AUTO_RESUME_BLOCKERS must be a mutable set"
        )
    blockers.add(_SENTINEL_FAILED_ATTENTION)
    if _SENTINEL_FAILED_ATTENTION not in blockers:
        raise RuntimeError("Sentinel refused the generic coding failure auto-resume blocker")


def install(
    agent: Any,
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
) -> None:
    _install_sentinel_failed_resume_guard()
    _install_convergence_review_fixes(cw, mission_epoch, convergence)
    _install_validation_persistence_fix(convergence)
    _install_policy(agent, cw, mission_epoch, convergence)
    _install_validation_side_effect_restamp(agent, cw, mission_epoch)
