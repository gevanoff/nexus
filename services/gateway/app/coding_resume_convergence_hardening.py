from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping


SCHEMA = "nexus_coding_resume_convergence.v1"
_SENTINEL_FAILED_ATTENTION = "run_failed"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
    """Return true when a replacement hypothesis still needs a consuming edit.

    Semantic rejection deliberately reopens execution. A fresh four-field
    hypothesis after that rejection is not itself an output mutation, so it must
    not be forced through validation before the new edit it is intended to
    justify.
    """
    material_update = _float(convergence._material_hypothesis_updated_at(task))
    if material_update <= 0:
        return False
    epoch_key = str(getattr(mission_epoch, "KEY", "coding_mission_acceptance_epoch"))
    epoch = _mapping(task.get(epoch_key))
    lifecycle = _mapping(task.get("agent_hypothesis_lifecycle"))
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
) -> Dict[str, Any]:
    state_key = hashlib.sha256(
        (
            f"{task_id}|{action_kind}|{threshold:.6f}|{validation_at:.6f}|"
            f"{review_at:.6f}|{diff_sha256}"
        ).encode("utf-8")
    ).hexdigest()
    return {
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
        "stage": f"post_edit_{action_kind}",
        "mission_acceptance_pending": True,
        "mission_diff_sha256": diff_sha256,
    }


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

    lifecycle = _mapping(task.get("agent_hypothesis_lifecycle"))
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

    # Reuse the semantic-acceptance implementation and state shape from the
    # preceding convergence layer once both durable prerequisites are current.
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


def _install_sentinel_failed_resume_guard() -> None:
    """Do not treat an untyped failed coding run as a transient retry signal.

    Gateway interruptions have their own typed recovery path. A generic failure
    can represent an intentional terminal blocker, semantic refusal, or a real
    defect. Until failures have a narrower recoverability taxonomy, Sentinel
    must leave them stable for human attention rather than immediately starting
    another runner attempt.
    """
    try:
        from app import sentinel_runtime
    except Exception:
        return
    blockers = getattr(sentinel_runtime, "_CODING_AUTO_RESUME_BLOCKERS", None)
    if isinstance(blockers, set):
        blockers.add(_SENTINEL_FAILED_ATTENTION)


def install(
    agent: Any,
    cw: Any,
    mission_epoch: Any,
    convergence: Any,
) -> None:
    _install_sentinel_failed_resume_guard()
    _install_policy(agent, cw, mission_epoch, convergence)
