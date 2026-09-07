from __future__ import annotations

import difflib
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from app import coding_validation_policy


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _current_head(cw: Any, task_id: str, task: Optional[Mapping[str, Any]] = None) -> str:
    try:
        result = cw.git_head(task_id)
    except Exception:
        result = {}
    if isinstance(result, Mapping):
        commit = str(result.get("commit") or "").strip()
        if commit:
            return commit
    source = task if isinstance(task, Mapping) else {}
    return str(
        source.get("last_commit")
        or source.get("last_checkpoint_commit")
        or source.get("agent_start_head")
        or ""
    ).strip()


def _has_prior_agent_history(task: Mapping[str, Any]) -> bool:
    runs = task.get("agent_runs") if isinstance(task.get("agent_runs"), list) else []
    if runs:
        return True
    return bool(
        task.get("agent_finished_at")
        or task.get("agent_run_id")
        or task.get("last_checkpoint_run_id")
    )


def _content_bound_untracked_diff(epoch: Any, cw: Any, *, repo: Path) -> tuple[str, str]:
    result = epoch._run_process(cw, ["git", "ls-files", "--others", "--exclude-standard"], cwd=repo)
    if not bool(result.get("ok")):
        return "", str(result.get("stderr") or result.get("error") or "git ls-files failed")
    paths = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip()]
    maximum = int(getattr(epoch, "_MAX_UNTRACKED", 200) or 200)
    if len(paths) > maximum:
        return "", f"mission delta has {len(paths)} untracked files; limit is {maximum}"

    size_limit = int(getattr(epoch, "_MAX_UNTRACKED_BYTES", 100_000) or 100_000)
    pieces: list[str] = []
    for raw in paths:
        candidate = repo.joinpath(raw).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            return "", f"untracked path escapes workspace: {raw}"
        if not candidate.is_file():
            continue
        try:
            data = candidate.read_bytes()
        except Exception as exc:
            return "", f"unable to read untracked file {raw}: {type(exc).__name__}: {exc}"

        digest = hashlib.sha256(data).hexdigest()
        identity = f"# nexus-untracked-content path={raw} size={len(data)} sha256={digest}"
        if len(data) > size_limit:
            pieces.append(
                f"diff --git a/{raw} b/{raw}\nnew file mode 100644\n{identity}\n"
                "Binary or oversized untracked file omitted from semantic review"
            )
            continue
        if b"\x00" in data:
            pieces.append(
                f"diff --git a/{raw} b/{raw}\nnew file mode 100644\n{identity}\n"
                "Binary untracked file omitted from semantic review"
            )
            continue

        text = data.decode("utf-8", errors="replace")
        rendered = "".join(
            difflib.unified_diff(
                [],
                text.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{raw}",
            )
        ).strip()
        # Bind the fingerprint to the exact bytes even when replacement decoding
        # would render two malformed UTF-8 byte sequences identically.
        pieces.append(f"{identity}\n{rendered}".strip())
    return "\n\n".join(piece for piece in pieces if piece).strip(), ""


def _worktree_dirty(cw: Any, task_id: str) -> bool:
    try:
        summary = cw.git_change_summary(task_id)
    except Exception:
        return False
    counts = _mapping(summary.get("counts")) if isinstance(summary, Mapping) else {}
    return int(counts.get("total") or 0) > 0


def _run_local_delta(cw: Any, task_id: str, task: Mapping[str, Any]) -> bool:
    current = _current_head(cw, task_id, task)
    start = str(task.get("agent_start_head") or "").strip()
    return bool(start and current and current != start) or _worktree_dirty(cw, task_id)


def _resumable_guard_failure(
    cw: Any,
    task_id: str,
    *,
    summary: str,
) -> Dict[str, Any]:
    error = "mission_acceptance_state_unavailable"
    required_action = (
        "Resume the run and retry coding_finish after mission acceptance state is available."
    )
    now = time.time()
    finalization_error = f"{error}: {summary}"
    result: Dict[str, Any] = {
        "ok": False,
        "success": False,
        "error": error,
        "finalization_status": "interrupted",
        "finalization_error": finalization_error,
        "stop_reason_code": "run_interrupted",
        "retryable": True,
        "required_action": required_action,
        "summary": summary,
        "finished_at": now,
    }

    def persist(latest: Dict[str, Any]) -> None:
        latest.update(
            {
                "finalization_status": "interrupted",
                "finalization_error": finalization_error,
                "terminal_result": dict(result),
            }
        )

    try:
        mutate = getattr(cw, "mutate_task", None)
        if callable(mutate):
            mutate(task_id, persist)
        else:
            latest = cw.load_task(task_id)
            persist(latest)
            cw.save_task(latest)
    except Exception:
        # The typed return remains authoritative when metadata persistence is
        # itself part of the transient outage.
        pass
    return result


def _accepted_inherited_state(
    epoch: Any,
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    task: Mapping[str, Any],
) -> Dict[str, Any]:
    if _run_local_delta(cw, task_id, task):
        return {}
    state = epoch.mission_delta_state(cw, task_id, dict(task))
    if not state.get("ok") or not state.get("has_delta"):
        return {}
    accepted = epoch._epoch_accepted_for_current(
        terminal_hardening,
        cw,
        agent,
        task_id,
        task,
    )
    return dict(state) if accepted else {}


def _record_final_head(epoch: Any, cw: Any, task_id: str, final_head: str) -> None:
    final_head = str(final_head or "").strip()
    if not final_head:
        return
    now = time.time()

    def apply(latest: Dict[str, Any]) -> None:
        current = dict(_mapping(latest.get(epoch.KEY)))
        if str(current.get("schema") or "") != str(epoch.SCHEMA):
            return
        if not current.get("accepted_fingerprint"):
            return
        current.update(
            {
                "status": "finalized",
                "accepted_head": final_head,
                "finalized_head": final_head,
                "finalized_at": float(current.get("finalized_at") or now),
                "updated_at": now,
            }
        )
        latest[epoch.KEY] = current

    mutate = getattr(cw, "mutate_task", None)
    if callable(mutate):
        mutate(task_id, apply)
        return
    latest = cw.load_task(task_id)
    apply(latest)
    cw.save_task(latest)


def _finalize_inherited_delta(
    *,
    epoch: Any,
    terminal_hardening: Any,
    cw: Any,
    agent: Any,
    task_id: str,
    task: Mapping[str, Any],
    state: Mapping[str, Any],
    mission: Optional[Dict[str, Any]],
    git_token_value: Optional[str],
    finish_summary: str,
) -> Dict[str, Any]:
    """Publish an accepted checkpointed delta without laundering concurrent changes."""

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
            raise RuntimeError("workspace changed during inherited mission finalization")
        current_head = str(head.get("commit") or "").strip()
        if not current_head:
            raise RuntimeError("successful mission has no branch commit")
        if current_head != str(state.get("current_head") or "").strip():
            raise RuntimeError("workspace HEAD changed during inherited mission finalization")

        # Recompute the full mission fingerprint after the repository audit and
        # before any push/PR side effect. A changed file or commit invalidates the
        # previously recorded semantic acceptance rather than being published.
        latest = cw.load_task(task_id)
        if not epoch._epoch_accepted_for_current(
            terminal_hardening,
            cw,
            agent,
            task_id,
            latest,
        ):
            raise RuntimeError("mission delta changed after semantic acceptance")

        if (
            coding_validation_policy.requires_agent_validation(latest)
            and bool(completion.get("require_validation_after_edit", True))
        ):
            validation = _mapping(snapshot.get("validation"))
            if not bool(validation.get("validation_after_latest_edit")):
                raise RuntimeError("successful mission lacks validation after the latest edit")
            if validation.get("last_validation_ok") is not True:
                raise RuntimeError("successful mission has a failed latest validation")
        if bool(completion.get("require_diff_review_after_edit", True)):
            review = _mapping(snapshot.get("diff_review"))
            if not bool(review.get("diff_reviewed_after_latest_edit")):
                raise RuntimeError("successful mission lacks diff review after the latest edit")

        result["final_commit"] = current_head
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
    mutate = getattr(cw, "mutate_task", None)
    if callable(mutate):
        mutate(
            task_id,
            lambda latest: latest.update(
                {
                    "mission": contract,
                    "terminal_result": result,
                    **result,
                }
            ),
        )
    else:
        latest = cw.load_task(task_id)
        latest.update({"mission": contract, "terminal_result": result, **result})
        cw.save_task(latest)
    if bool(result.get("ok")):
        _record_final_head(epoch, cw, task_id, str(result.get("final_commit") or ""))
    return result


def install(
    epoch: Any,
    agent: Any,
    guarded: Any,
    cw: Any,
    terminal_hardening: Any,
) -> None:
    """Close content-binding, baseline, and finalization integrity gaps."""
    if bool(getattr(guarded, "_mission_acceptance_integrity_installed", False)):
        return

    original_resolve_base = epoch._resolve_acceptance_base

    def resolve_acceptance_base(cw_obj: Any, task_id: str, task: Mapping[str, Any]) -> str:
        if not _has_prior_agent_history(task):
            current = _current_head(cw_obj, task_id, task)
            if current:
                return current
        return original_resolve_base(cw_obj, task_id, task)

    epoch._resolve_acceptance_base = resolve_acceptance_base
    epoch._safe_untracked_diff = lambda cw_obj, *, repo: _content_bound_untracked_diff(
        epoch,
        cw_obj,
        repo=repo,
    )

    prior_finalize = agent.finalize_successful_run

    def finalize_successful_run(
        task_id: str,
        *,
        mission: Optional[Dict[str, Any]] = None,
        git_token_value: Optional[str] = None,
        finish_summary: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        try:
            workspace_lock = cw.task_workspace_lock(task_id)
        except Exception:
            return _resumable_guard_failure(
                cw,
                task_id,
                summary=(
                    "The task workspace serialization boundary is unavailable during "
                    "mission-integrity finalization. Finalization was interrupted before "
                    "repository side effects and may be resumed."
                ),
            )

        # This integrity wrapper is installed after the mission-epoch finalizer.
        # Keep the same re-entrant task lock outermost here so inherited-delta
        # publication cannot bypass the acceptance-check -> push/PR boundary.
        with workspace_lock:
            try:
                task = cw.load_task(task_id)
                inherited = _accepted_inherited_state(
                    epoch,
                    terminal_hardening,
                    cw,
                    agent,
                    task_id,
                    task,
                )
            except Exception:
                return _resumable_guard_failure(
                    cw,
                    task_id,
                    summary=(
                        "Inherited mission acceptance state could not be established during "
                        "finalization. Finalization was interrupted before repository side "
                        "effects and may be resumed."
                    ),
                )

            if inherited:
                return _finalize_inherited_delta(
                    epoch=epoch,
                    terminal_hardening=terminal_hardening,
                    cw=cw,
                    agent=agent,
                    task_id=task_id,
                    task=task,
                    state=inherited,
                    mission=mission,
                    git_token_value=git_token_value,
                    finish_summary=finish_summary,
                )

            # The prior mission-epoch finalizer acquires this same RLock. Re-entry
            # is intentional and keeps every active finalizer layer serialized.
            result = prior_finalize(
                task_id,
                mission=mission,
                git_token_value=git_token_value,
                finish_summary=finish_summary,
                run_id=run_id,
            )
            if result.get("ok") is True:
                final_head = (
                    str(result.get("final_commit") or "").strip()
                    or _current_head(cw, task_id)
                )
                _record_final_head(epoch, cw, task_id, final_head)
            return result

    agent.finalize_successful_run = finalize_successful_run
    guarded._mission_acceptance_integrity_installed = True
