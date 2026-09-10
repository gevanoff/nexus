from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, Mapping

from app import coding_validation_policy


log = logging.getLogger(__name__)

SCHEMA = "nexus_coding_resume_convergence.v1"
_SENTINEL_FAILED_ATTENTION = "run_failed"
_VALIDATION_KEY = "coding_validation_provenance"
_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
EDIT_BATCH_KEY = "coding_edit_batch"
EDIT_BATCH_LIMIT = 4
_STRUCTURED_EDITS = {"coding_write_file", "coding_replace_text", "coding_apply_patch"}


def _grounded_batch_policy(agent: Any, task: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    if state:
        return dict(state)
    if not _mapping(task.get("project_plan")).get("note") or not task.get("agent_events"):
        return {}
    # A grounded plan can be recorded during ordinary execution, before any
    # stagnation/forced-action event. Reuse the same evidence and durable-note
    # validators to qualify a batch; forcing stagnation first is not necessary.
    from app import coding_evidence_policy, coding_hypothesis_persistence
    base = coding_hypothesis_persistence._base_policy(agent)
    if not hasattr(base, "_HYPOTHESIS_FIELDS"):
        return {}
    candidate = {
        "requires_hypothesis": True, "action_kind": "edit",
        "canonical_action_kind": "edit", "activation_plan_revision": -1,
        "activation_event_count": len(task.get("agent_events") or []),
        "run_id": str(task.get("agent_run_id") or ""),
    }
    qualified = coding_evidence_policy.apply_provenance_gate(base, task, candidate)
    return coding_hypothesis_persistence._durable_note_state(agent, coding_evidence_policy, task, qualified)


def active_edit_batch(task: Mapping[str, Any]) -> Dict[str, Any]:
    batch = _mapping(task.get(EDIT_BATCH_KEY))
    if batch.get("status") != "active" or batch.get("schema") != "nexus_coding_edit_batch.v1":
        return {}
    plan = _mapping(task.get("project_plan"))
    note_sha = hashlib.sha256(str(plan.get("note") or "").strip().encode()).hexdigest()
    if batch.get("note_sha256") != note_sha or batch.get("plan_revision") != plan.get("revision"):
        return {}
    try:
        attempts = int(batch.get("attempts") or 0)
    except (ValueError, TypeError):
        return {}
    paths = batch.get("paths")
    if not isinstance(paths, list) or not 2 <= len(paths) <= EDIT_BATCH_LIMIT or not all(isinstance(path, str) and path for path in paths):
        return {}
    if not 0 < attempts < EDIT_BATCH_LIMIT:
        return {}
    return dict(batch)


def _batch_paths(cw: Any, name: str, args: Mapping[str, Any]) -> set[str]:
    if name == "coding_apply_patch":
        return set(cw._patch_paths(str(args.get("patch") or "")))
    return {str(args.get("path") or "").strip().replace("\\", "/")}


def _close_edit_batch(cw: Any, task_id: str, reason: str) -> None:
    def apply(task: Dict[str, Any]) -> None:
        batch = dict(_mapping(task.get(EDIT_BATCH_KEY)))
        if batch.get("status") == "active":
            batch.update(status="closed", closed_at=time.time(), close_reason=reason)
            task[EDIT_BATCH_KEY] = batch
    cw.mutate_task(task_id, apply)


def _record_edit_attempt(
    cw: Any, task_id: str, before: Mapping[str, Any], state: Mapping[str, Any],
    name: str, args: Mapping[str, Any], *, mutated: bool,
) -> None:
    if name not in _STRUCTURED_EDITS or args.get("check_only"):
        return
    batch = active_edit_batch(before)
    if batch:
        batch["attempts"] += 1
        if mutated:
            batch["mutations"] += 1
        if batch["attempts"] >= EDIT_BATCH_LIMIT:
            batch.update(status="closed", closed_at=time.time(), close_reason="attempt_limit")
    else:
        # Only a new, repository-grounded multi-file hypothesis can open a batch.
        # Plan churn and an unchanged consumed hypothesis cannot replenish it.
        plan = _mapping(before.get("project_plan"))
        note_sha = hashlib.sha256(str(plan.get("note") or "").strip().encode()).hexdigest()
        previous = _mapping(before.get(EDIT_BATCH_KEY))
        lifecycle = _mapping(before.get(_LIFECYCLE_KEY))
        paths = sorted(set(state.get("durable_hypothesis_note_causal_targets") or []))
        if (
            not mutated
            or state.get("action_kind") != "edit"
            or not state.get("hypothesis_causal_evidence_linked")
            or not state.get("durable_hypothesis_note_ready")
            or not 2 <= len(paths) <= EDIT_BATCH_LIMIT
            or previous.get("note_sha256") == note_sha
            or lifecycle.get("note_fingerprint") == note_sha
            or not _batch_paths(cw, name, args).issubset(paths)
        ):
            return
        batch = {
            "schema": "nexus_coding_edit_batch.v1", "status": "active",
            "note_sha256": note_sha, "plan_revision": plan.get("revision"),
            "paths": paths, "attempts": 1, "mutations": 1,
            "attempt_limit": EDIT_BATCH_LIMIT, "opened_at": time.time(),
            "opened_run_id": str(before.get("agent_run_id") or ""),
            "qualified_policy": dict(state),
        }
    def persist(task: dict[str, Any]) -> None:
        task[EDIT_BATCH_KEY] = batch
        if batch["attempts"] == 1:
            from app import coding_hypothesis_persistence
            lifecycle = dict(_mapping(task.get(_LIFECYCLE_KEY)))
            if lifecycle.get("note_fingerprint") == batch["note_sha256"]:
                lifecycle["causal_evidence_targets"] = list(batch["paths"])
                lifecycle["causal_evidence_ranges"] = list(state.get("causal_evidence_ranges") or [])
                lifecycle["verified_evidence_digest"] = coding_hypothesis_persistence._verified_evidence_digest(before, state)
                task[_LIFECYCLE_KEY] = lifecycle
    cw.mutate_task(task_id, persist)


def _coherent_edit_state(task: Mapping[str, Any], batch: Mapping[str, Any]) -> Dict[str, Any]:
    qualified = dict(_mapping(batch.get("qualified_policy")))
    allowed = [*sorted(_STRUCTURED_EDITS), "coding_read_file_lines", "coding_finish"]
    if coding_validation_policy.requires_agent_validation(task):
        allowed.append("coding_run_command")
        close_action = "Start targeted validation with coding_run_command to close the batch early."
    else:
        allowed.append("coding_git_diff")
        close_action = "Call coding_git_diff to close the batch and review; the trusted runner validates later."
    qualified.update(_state(
        task_id=str(task.get("id") or ""), action_kind="edit",
        required_action=(
            f"Complete the coherent repair on {', '.join(batch['paths'])}. "
            f"{EDIT_BATCH_LIMIT - batch['attempts']} structured edit attempts remain. "
            "Reads are limited to these paths. Broad investigation and plan churn are disabled. "
            f"{close_action} Every mutation invalidates validation and diff review. "
            "If the hypothesis is contradicted, use coding_refute_hypothesis; "
            "coding_finish is available only with success=false and a concrete blocker."
        ),
        allowed_tools=allowed + ["coding_refute_hypothesis"],
        threshold=_float(batch.get("opened_at")), diff_sha256="",
        stage="coherent_edit_batch",
        extra={"edit_batch_paths": list(batch["paths"]), "edit_batch_attempts": batch["attempts"], "hypothesis_note_sha256": batch.get("note_sha256")},
    ))
    # Progress keys must change when an attempt is spent, even after a no-op.
    qualified["state_key"] += f":{batch['attempts']}"
    return qualified


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


def post_edit_state(
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
    task: Mapping[str, Any],
) -> Dict[str, Any]:
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
    if _active_refutation(task, mission_epoch):
        return {}
    batch = active_edit_batch(task)
    if batch:
        return _coherent_edit_state(task, batch)
    if _pending_replacement_hypothesis(convergence, mission_epoch, task):
        return {}

    threshold = _float(convergence._readiness_threshold(task, mission_epoch))
    if threshold <= 0:
        return {}
    if convergence._latest_decisive_rejection(task, threshold):
        return {}
    if convergence._semantic_rejection_guard_blocks(cw, mission_epoch, task_id, task):
        return {}

    try:
        delta = mission_epoch.mission_delta_state(cw, task_id, dict(task))
    except Exception:
        return {}
    if not delta.get("ok") or not delta.get("has_delta"):
        return {}
    diff_sha = str(delta.get("diff_sha256") or "")

    declared_validation_deferred = not (
        coding_validation_policy.requires_agent_validation(task)
    )
    if declared_validation_deferred:
        # Harness fixtures are validated by the trusted runner after the agent
        # reaches a terminal state. The agent cannot run arbitrary commands, so
        # its post-edit convergence obligation advances directly to diff review.
        validation_ready, validation_at = True, 0.0
    else:
        validation_ready, validation_at = convergence._validation_ready(
            task, threshold
        )
    if not validation_ready:
        unresolved = convergence._unresolved_validation_failures(task, threshold)
        if unresolved:
            refutation_tool = str(
                getattr(mission_epoch, "REFUTATION_TOOL", "coding_refute_hypothesis")
            )
            labels = [" ".join(signature) for _ts, signature in unresolved[-3:]]
            failing = "; ".join(labels)
            required = (
                "Post-edit validation failed and the failure is still unresolved. Repair the "
                "smallest evidence-backed defect with structured edit tools, materially revise "
                "the plan if the causal hypothesis changed, or rerun the same failing validation "
                f"after an environmental correction. Unresolved validation: {failing}. Do not "
                "substitute a weaker green check for the failing signature. If the failing "
                f"validation contradicts the consumed causal hypothesis, {refutation_tool} is "
                "explicitly available."
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
                    refutation_tool,
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
        return _state(
            task_id=task_id,
            action_kind="validate",
            required_action=(
                "The pending mission delta has not passed validation after its latest mutation. "
                "Run one targeted validation command now. Do not inspect, edit, revise the plan, "
                "or review the diff first. If validation cannot be run, call coding_finish with "
                "success=false and a concrete blocker."
            ),
            allowed_tools=["coding_run_command", "coding_finish"],
            threshold=threshold,
            diff_sha256=diff_sha,
            validation_at=validation_at,
        )

    review_at = convergence._latest_diff_review_at(task, threshold)
    if not review_at:
        if declared_validation_deferred:
            required_action = (
                "The pending harness mission delta has not been diff-reviewed "
                "after its latest mutation. Declared fixture validation is "
                "deferred to the trusted runner after the agent reaches a "
                "terminal state. Call coding_git_diff now."
            )
            extra = {"declared_validation_deferred": True}
        else:
            required_action = (
                "The pending mission delta has passed post-mutation validation "
                "but has not been diff-reviewed after its latest mutation. Call "
                "coding_git_diff now. Do not reopen inspection, edit, or plan "
                "work before reviewing the diff."
            )
            extra = None
        return _state(
            task_id=task_id,
            action_kind="review",
            required_action=required_action,
            allowed_tools=["coding_git_diff", "coding_finish"],
            threshold=threshold,
            diff_sha256=diff_sha,
            validation_at=validation_at,
            extra=extra,
        )
    return dict(convergence._terminal_state(cw, mission_epoch, task) or {})


def _install_policy(agent: Any, cw: Any, mission_epoch: Any, convergence: Any) -> None:
    policy = getattr(agent, "forced_action", None)
    if policy is None or bool(getattr(policy, "_coding_resume_convergence_installed", False)):
        return
    prior_active = getattr(policy, "active_state", None)
    if not callable(prior_active):
        return

    def active_state_with_resume_convergence(task: Mapping[str, Any]) -> Dict[str, Any]:
        derived = post_edit_state(cw, mission_epoch, convergence, task)
        return derived if derived else dict(prior_active(task) or {})

    policy.active_state = active_state_with_resume_convergence
    policy._coding_active_state_before_resume_convergence = prior_active

    prior_prompt = getattr(policy, "prompt_context", None)
    if callable(prior_prompt):
        def prompt_context_with_resume_convergence(task: Mapping[str, Any]) -> str:
            state = policy.active_state(task)
            if str(state.get("schema") or "") == SCHEMA:
                if state.get("stage") == "coherent_edit_batch":
                    return "Controller coherent edit batch is ACTIVE. " + str(state["required_action"])
                if state.get("validation_repair") is True:
                    failing = "; ".join(state.get("unresolved_validation_signatures") or [])
                    return (
                        "Controller post-edit validation-repair mode is ACTIVE. A substantive "
                        f"validation failure remains unresolved: {failing}. Repair it with the "
                        "smallest structured edit, revise the plan only if the causal hypothesis "
                        "changed, or rerun that same validation after an environmental correction. "
                        "coding_run_command accepts only validation commands in forced states; "
                        "make repairs with the structured edit tools."
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
                    if state.get("declared_validation_deferred") is True:
                        return (
                            "Controller post-edit convergence is ACTIVE. The "
                            "complete pending harness mission delta needs diff "
                            "review. Declared fixture validation will run in the "
                            "trusted runner after terminal agent state. Call "
                            "coding_git_diff now."
                        )
                    return (
                        "Controller post-edit convergence is ACTIVE. Validation is current and the "
                        "complete pending mission delta now needs diff review. Call coding_git_diff "
                        "now; do not inspect, edit, or revise the plan first."
                    )
            return str(prior_prompt(task) or "")
        policy.prompt_context = prompt_context_with_resume_convergence
        policy._coding_prompt_before_resume_convergence = prior_prompt
    policy._coding_resume_convergence_installed = True
    prior_specs = getattr(agent, "_tool_specs_for_task", None)
    if callable(prior_specs):
        def specs_with_blocker_finish(task: Dict[str, Any]) -> list[Any]:
            specs = list(prior_specs(task))
            state = policy.active_state(task)
            if str(state.get("action_kind") or "") not in {"validate", "review", "diff_review"} and state.get("stage") != "coherent_edit_batch":
                return specs
            out = []
            for spec in specs:
                if spec.function.name == "coding_finish":
                    spec = agent.ToolSpec(function=agent.ToolFunction(
                        name="coding_finish",
                        description="Stop with a concrete blocker. Successful finish is disabled: " + str(state.get("required_action") or ""),
                        parameters={
                            "type": "object", "required": ["success", "summary"],
                            "properties": {
                                "success": {"type": "boolean", "const": False, "enum": [False]},
                                "summary": {"type": "string", "minLength": 8, "description": "Concrete blocker preventing the required action."},
                            },
                        },
                    ))
                out.append(spec)
            return out
        agent._tool_specs_for_task = specs_with_blocker_finish


def _tracked_diff_sha(cw: Any, mission_epoch: Any, task_id: str) -> tuple[bool, str]:
    """Return (ok, sha) for the tracked-only portion of the mission delta."""
    try:
        delta = mission_epoch.mission_delta_state(cw, task_id)
    except Exception:
        return False, ""
    if not delta.get("ok"):
        return False, ""
    return True, str(delta.get("tracked_diff_sha256") or "")


def _restamp_validation_after_workspace_mutation(
    cw: Any,
    mission_epoch: Any,
    task_id: str,
    argv: Any,
) -> None:
    """Re-stamp a validation whose only side effects were untracked artifacts.

    Callers must verify the tracked mission diff is unchanged before invoking
    this: a validation that rewrote tracked source did not verify the tree it
    produced, so its provenance must stay stale and force a re-run.
    """
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


def _install_validation_side_effect_restamp(agent: Any, cw: Any, mission_epoch: Any) -> None:
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
        task = cw.load_task(task_id)
        batch = active_edit_batch(task)
        state = agent.forced_action.active_state(task)
        batch_policy = _grounded_batch_policy(agent, task, state) if name in _STRUCTURED_EDITS else state
        # A call may have been authorized before another call spent the last
        # attempt. Recheck closed batches under the mutation lock as well.
        if batch or (name in _STRUCTURED_EDITS and task.get(EDIT_BATCH_KEY)):
            from app.coding_forced_action import call_allowed_in_state
            if not call_allowed_in_state(state, name=name, args=args, is_validation_command=coding_work_phases.is_validation_command):
                return {
                    "ok": False, "success": False, "error": "forced_action_tool_rejected",
                    "message": "This call is disabled by the current controller action policy. It was not executed.",
                    "required_action": state.get("required_action"),
                }
        # Guard the public dispatch boundary too: callers must not be able to
        # skip the runner's evaluation and consume a semantic reviewer attempt.
        if name == "coding_finish" and str(state.get("action_kind") or "") in {"validate", "review", "diff_review"}:
            from app.coding_forced_action import call_allowed_in_state
            if not call_allowed_in_state(state, name=name, args=args, is_validation_command=coding_work_phases.is_validation_command):
                return {
                    "ok": False, "success": False, "error": "forced_action_tool_rejected",
                    "message": "Successful finish is disabled until the required controller action completes.",
                    "required_action": state.get("required_action"),
                    "action_kind": state.get("action_kind"),
                    "allowed_tools": state.get("allowed_tools"),
                }
        if batch and name in {"coding_run_command", "coding_git_diff", "coding_finish"}:
            _close_edit_batch(cw, task_id, name)
        elif _mapping(task.get(EDIT_BATCH_KEY)).get("status") == "active" and not batch:
            _close_edit_batch(cw, task_id, "hypothesis_changed")
        argv = args.get("argv") if str(name or "") == "coding_run_command" else None
        try:
            qualifies = bool(argv is not None and coding_work_phases.is_validation_command(argv))
        except Exception:
            qualifies = False
        before_ok, before_tracked = (
            _tracked_diff_sha(cw, mission_epoch, task_id) if qualifies else (False, "")
        )
        result = prior_run_tool(task_id, name, args, git_token_value=git_token_value)
        if batch and name == "coding_refute_hypothesis" and result.get("refuted"):
            _close_edit_batch(cw, task_id, name)
        mutation_checker = getattr(agent, "_tool_result_modified_workspace", None)
        mutated = bool(mutation_checker(name, args, result)) if callable(mutation_checker) else result.get("workspace_modified") is True
        _record_edit_attempt(cw, task_id, task, batch_policy, name, args, mutated=mutated)
        if qualifies and result.get("workspace_modified") is True and before_ok:
            # Re-stamp freshness only when the validation's side effects were
            # invisible to the tracked mission diff (caches, coverage files).
            # A tracked-source mutation (fix-mode linters, snapshot-updating
            # test runs) leaves provenance stale so validation must re-run
            # against the tree it actually produced.
            after_ok, after_tracked = _tracked_diff_sha(cw, mission_epoch, task_id)
            if after_ok and after_tracked == before_tracked:
                _restamp_validation_after_workspace_mutation(
                    cw, mission_epoch, task_id, argv
                )
        return result

    def run_tool_serialized(task_id: str, name: str, args: Dict[str, Any], *, git_token_value: Any) -> Dict[str, Any]:
        from app.coding_plan_edit_serialization import _task_lock
        # The batch budget must be spent in the same critical section as the
        # mutation and plan revalidation, including callers outside the runner.
        with _task_lock(task_id):
            return run_tool_with_validation_restamp(task_id, name, args, git_token_value=git_token_value)

    agent._run_tool = run_tool_serialized
    guarded._run_tool_with_semantic_acceptance = run_tool_serialized
    agent._coding_run_tool_before_pr93_validation_restamp = prior_run_tool
    agent._coding_pr93_validation_restamp_installed = True


def _install_sentinel_failed_resume_guard() -> None:
    """Verify the Sentinel auto-resume blocker without risking gateway boot.

    ``run_failed`` is declared in ``sentinel_runtime._CODING_AUTO_RESUME_BLOCKERS``
    itself; this guard only repairs and reports drift. Supervision policy
    problems must never take the Coding API down with them.
    """
    try:
        from app import sentinel_runtime
    except Exception:
        log.exception("Sentinel unavailable while verifying coding auto-resume blockers")
        return
    raw = getattr(sentinel_runtime, "_CODING_AUTO_RESUME_BLOCKERS", None)
    if isinstance(raw, set) and _SENTINEL_FAILED_ATTENTION in raw:
        return
    log.error(
        "Sentinel auto-resume blockers drifted (missing %s); repairing in place",
        _SENTINEL_FAILED_ATTENTION,
    )
    try:
        blockers = set(raw or ())
    except TypeError:
        blockers = set()
    blockers.add(_SENTINEL_FAILED_ATTENTION)
    sentinel_runtime._CODING_AUTO_RESUME_BLOCKERS = blockers


def install(agent: Any, cw: Any, mission_epoch: Any, convergence: Any) -> None:
    policy = getattr(agent, "forced_action", None)
    if policy is not None and not bool(
        getattr(policy, "_coding_terminal_convergence_installed", False)
    ):
        # This layer must wrap the acceptance-convergence policy to take
        # precedence over it. Installing in the wrong order would silently
        # invert derived-state priority, so fail loudly at install time.
        raise RuntimeError(
            "coding_resume_convergence_hardening.install() requires "
            "coding_acceptance_convergence_hardening.install() to run first"
        )
    _install_sentinel_failed_resume_guard()
    _install_policy(agent, cw, mission_epoch, convergence)
    _install_validation_side_effect_restamp(agent, cw, mission_epoch)
