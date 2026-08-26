from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping


_TERMINAL_SCHEMA = "nexus_coding_terminal_convergence.v1"
_TERMINAL_ACTION = "finish"
_VALIDATION_KEY = "coding_validation_provenance"
_LIFECYCLE_KEY = "agent_hypothesis_lifecycle"
_HYPOTHESIS_LABELS = (
    "Root cause:",
    "Repository evidence:",
    "Competing explanation checked:",
    "Expected result:",
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


def _structured_hypothesis_fingerprint(task: Mapping[str, Any]) -> str:
    plan = _mapping(task.get("project_plan"))
    raw_note = str(plan.get("note") or "")
    note = raw_note.strip()
    if not note or not all(label in note for label in _HYPOTHESIS_LABELS):
        return ""
    return hashlib.sha256(raw_note.encode("utf-8")).hexdigest()


def _material_hypothesis_updated_at(task: Mapping[str, Any]) -> float:
    """Return plan update time only for a replacement four-field hypothesis.

    Project-plan bookkeeping is intentionally broader than causal state: item status,
    summaries, and generic notes may all advance ``updated_at`` after validation and
    diff review. Those updates must not reopen broad tools. A fresh acceptance pass is
    required only when the current structured remediation hypothesis differs from the
    hypothesis consumed by the latest repository mutation.
    """
    lifecycle = _mapping(task.get(_LIFECYCLE_KEY))
    if str(lifecycle.get("status") or "") != "consumed":
        return 0.0
    consumed_fingerprint = str(lifecycle.get("note_fingerprint") or "").strip()
    current_fingerprint = _structured_hypothesis_fingerprint(task)
    if not consumed_fingerprint or not current_fingerprint:
        return 0.0
    if current_fingerprint == consumed_fingerprint:
        return 0.0

    plan = _mapping(task.get("project_plan"))
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


def _validation_ready(task: Mapping[str, Any], threshold: float) -> tuple[bool, float]:
    validation = _mapping(task.get(_VALIDATION_KEY))
    ts = _float(validation.get("ts"))
    ready = bool(
        str(validation.get("schema") or "") == "nexus_coding_validation_provenance.v1"
        and validation.get("ok") is True
        and ts
        and ts >= threshold
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


def _latest_semantic_review(task: Mapping[str, Any], threshold: float) -> Mapping[str, Any]:
    for raw in reversed(list(task.get("agent_events") or [])):
        event = _mapping(raw)
        if str(event.get("type") or "") != "semantic_acceptance_review":
            continue
        if _float(event.get("ts")) < threshold:
            continue
        return event
    return {}


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

    prior_review = _latest_semantic_review(task, threshold)
    if prior_review:
        # A rejected acceptance review must reopen execution so the agent can
        # materially change the diff/hypothesis/evidence. An accepted review is
        # already on the normal finalization path and must not be re-requested.
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
            "summary": "coding_refute_hypothesis is available only while the live effective policy authorizes forced edit mode.",
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
        if str(name or "") == refutation_tool:
            return _run_live_refutation(
                agent,
                cw,
                mission_epoch,
                task_id,
                args,
            )
        return prior_run_tool(
            task_id,
            name,
            args,
            git_token_value=git_token_value,
        )

    agent._run_tool = run_tool_with_live_refutation
    guarded._run_tool_with_semantic_acceptance = run_tool_with_live_refutation
    agent._coding_run_tool_before_live_refutation = prior_run_tool
    agent._coding_live_refutation_execution_installed = True


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
    _install_live_refutation(agent, guarded, cw, mission_epoch)
    _install_terminal_policy(agent, cw, mission_epoch)
    _install_semantic_evidence_instruction(semantic_acceptance)
