from __future__ import annotations

import time
from typing import Any, Dict, Mapping, Optional


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _inherited_mission_delta(
    continuity: Any,
    cw: Any,
    agent: Any,
    coding_run_delta: Any,
    task_id: str,
    task: Mapping[str, Any],
) -> Dict[str, Any]:
    if not continuity._existing_epoch(task):
        return {}
    if continuity._run_local_delta(cw, task_id, task):
        return {}
    state = continuity.mission_acceptance_state(
        cw,
        agent,
        coding_run_delta,
        task_id,
        task,
    )
    return state if bool(state.get("has_delta")) else {}


def _finalize_inherited_delta(
    *,
    continuity: Any,
    cw: Any,
    agent: Any,
    coding_run_delta: Any,
    task_id: str,
    mission: Optional[Dict[str, Any]],
    git_token_value: Optional[str],
    finish_summary: str,
) -> Dict[str, Any]:
    """Finalize an already-checkpointed mission delta without falsifying run provenance."""
    task = cw.load_task(task_id)
    state = _inherited_mission_delta(
        continuity,
        cw,
        agent,
        coding_run_delta,
        task_id,
        task,
    )
    if not state:
        raise RuntimeError("inherited mission finalization requested without an inherited mission delta")

    contract = cw.normalize_coding_mission(task, mission)
    publish = _mapping(contract.get("publish_policy"))
    completion = _mapping(contract.get("completion_policy"))
    now = time.time()
    result: Dict[str, Any] = {
        "ok": False,
        "finalization_status": "running",
        "final_commit": "",
        "committed_at": None,
        "pushed_at": None,
        "push_result": None,
        "pr_url": "",
        "pr_number": None,
        "pr_created_at": None,
        "finalization_error": "",
    }

    try:
        status = cw.git_status(task_id, git_token_value=git_token_value)
        diff = cw.git_diff(task_id)
        changes = cw.git_change_summary(task_id)
        snapshot = cw.coding_state_snapshot(task_id)
        head = cw.git_head(task_id)
        if not status.get("ok") or not diff.get("ok") or not changes.get("ok"):
            raise RuntimeError("final repository audit failed")

        counts = _mapping(changes.get("counts"))
        if int(counts.get("total") or 0) > 0:
            # A concurrent/current-run mutation appeared after dispatch selected
            # the inherited-only path. Fail closed rather than silently publish
            # a delta that this path did not commit.
            raise RuntimeError("workspace changed during inherited mission finalization")

        current_head = str(head.get("commit") or "").strip()
        if not current_head:
            raise RuntimeError("successful mission has no branch commit")
        if current_head != str(state.get("current_head") or "").strip():
            raise RuntimeError("workspace HEAD changed during inherited mission finalization")

        if bool(completion.get("require_validation_after_edit", True)):
            validation = _mapping(snapshot.get("validation"))
            if not bool(validation.get("validation_after_latest_edit")):
                raise RuntimeError("successful mission lacks validation after the latest edit")
            if validation.get("last_validation_ok") is not True:
                raise RuntimeError("successful mission has a failed latest validation")

        if bool(completion.get("require_diff_review_after_edit", True)):
            diff_review = _mapping(snapshot.get("diff_review"))
            if not bool(diff_review.get("diff_reviewed_after_latest_edit")):
                raise RuntimeError("successful mission lacks diff review after the latest edit")

        result["final_commit"] = current_head
        latest = cw.load_task(task_id)
        result["committed_at"] = float(
            latest.get("last_checkpoint_at") or latest.get("updated_at") or now
        )
        if bool(completion.get("require_commit_on_success", True)) and not result["final_commit"]:
            raise RuntimeError("successful mission has no final commit")

        if publish.get("push") == "on_success" or publish.get("draft_pr") == "on_success":
            push = cw.push_task(
                task_id,
                remote=str(publish.get("remote") or "origin"),
                git_token_value=git_token_value,
            )
            result["push_result"] = push
            if not bool(push.get("ok")):
                result["finalization_status"] = "failed_publish"
                raise RuntimeError(str(push.get("stderr") or push.get("error") or "push failed"))
            result["pushed_at"] = time.time()

        if publish.get("draft_pr") == "on_success":
            pr = cw.create_pull_request(
                task_id,
                title=str(publish.get("pr_title") or finish_summary or "Nexus coding mission")
                .splitlines()[0][:200],
                body=str(publish.get("pr_body") or finish_summary or ""),
                draft=True,
                git_token_value=git_token_value,
            )
            if not bool(pr.get("ok")):
                result["finalization_status"] = "failed_publish"
                raise RuntimeError(str(pr.get("error") or "draft PR creation failed"))
            result["pr_url"] = str(
                pr.get("url") or pr.get("html_url") or pr.get("stdout") or ""
            ).strip()
            result["pr_number"] = pr.get("number")
            result["pr_created_at"] = time.time()

        result["ok"] = True
        result["finalization_status"] = "completed"
    except Exception as exc:
        if result["finalization_status"] == "running":
            result["finalization_status"] = "failed_finalization"
        result["finalization_error"] = f"{type(exc).__name__}: {exc}"

    result["stop_reason_code"] = (
        "run_completed"
        if result["finalization_status"] == "completed"
        else str(result["finalization_status"] or "failed_finalization")
    )
    result["finished_at"] = time.time()
    cw.mutate_task(
        task_id,
        lambda latest: latest.update(
            {
                "mission": contract,
                "terminal_result": result,
                **result,
            }
        ),
    )
    if bool(result.get("ok")):
        continuity._mark_epoch_accepted(cw, task_id, str(result.get("final_commit") or ""))
    return result


def install(
    agent: Any,
    guarded: Any,
    cw: Any,
    coding_run_delta: Any,
    continuity: Any,
) -> None:
    """Use a provenance-safe finalizer for inherited mission-level deltas."""
    if bool(getattr(guarded, "_mission_finalization_continuity_installed", False)):
        return

    prior_finalize = agent.finalize_successful_run

    def finalize_successful_run(
        task_id: str,
        *,
        mission: Optional[Dict[str, Any]] = None,
        git_token_value: Optional[str] = None,
        finish_summary: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        task = cw.load_task(task_id)
        inherited = _inherited_mission_delta(
            continuity,
            cw,
            agent,
            coding_run_delta,
            task_id,
            task,
        )
        if not inherited:
            return prior_finalize(
                task_id,
                mission=mission,
                git_token_value=git_token_value,
                finish_summary=finish_summary,
                run_id=run_id,
            )
        return _finalize_inherited_delta(
            continuity=continuity,
            cw=cw,
            agent=agent,
            coding_run_delta=coding_run_delta,
            task_id=task_id,
            mission=mission,
            git_token_value=git_token_value,
            finish_summary=finish_summary,
        )

    agent.finalize_successful_run = finalize_successful_run
    guarded._mission_finalization_continuity_installed = True
