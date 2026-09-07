from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, Mapping


log = logging.getLogger(__name__)

SCHEMA = "nexus_coding_resume_convergence.v1"
_SENTINEL_FAILED_ATTENTION = "run_failed"
_VALIDATION_KEY = "coding_validation_provenance"
_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"


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

    declared_validation_deferred = (
        str(task.get("kind") or "") == "harness_eval"
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
        argv = args.get("argv") if str(name or "") == "coding_run_command" else None
        try:
            qualifies = bool(argv is not None and coding_work_phases.is_validation_command(argv))
        except Exception:
            qualifies = False
        before_ok, before_tracked = (
            _tracked_diff_sha(cw, mission_epoch, task_id) if qualifies else (False, "")
        )
        result = prior_run_tool(task_id, name, args, git_token_value=git_token_value)
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

    agent._run_tool = run_tool_with_validation_restamp
    guarded._run_tool_with_semantic_acceptance = run_tool_with_validation_restamp
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
