from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from app import coding_workspace as cw
from app import coding_workspace_reconciliation as base


def _workspace_head(task_id: str, task: Dict[str, Any]) -> str:
    try:
        result = cw.git_head(task_id)
        commit = str(result.get("commit") or "").strip() if isinstance(result, dict) else ""
        if commit:
            return commit
    except Exception:
        pass
    return str(task.get("last_commit") or task.get("last_checkpoint_commit") or "").strip()


def _workspace_dirty(task_id: str) -> Optional[bool]:
    try:
        result = cw.git_change_summary(task_id)
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    return int(counts.get("total") or 0) > 0


def _pull_request_state(
    task: Dict[str, Any],
    *,
    pr_number: int,
    git_token_value: Optional[str],
) -> Dict[str, Any]:
    owner_repo = cw._github_owner_repo(str(task.get("repo_url") or ""))
    if owner_repo is None:
        return {
            "known": False,
            "integrated": False,
            "source": "github_pr",
            "pr_number": pr_number,
            "error": "repository URL is not a GitHub repository",
        }
    owner, repo = owner_repo
    result = cw._github_api_request(
        "GET",
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
        git_token_value=git_token_value,
    )
    if not result.get("ok"):
        return {
            "known": False,
            "integrated": False,
            "source": "github_pr",
            "pr_number": pr_number,
            "error": str(result.get("error") or result.get("body") or "GitHub PR lookup failed")[:1000],
        }
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    head = body.get("head") if isinstance(body.get("head"), dict) else {}
    merged = bool(body.get("merged_at") or body.get("merged"))
    return {
        "known": True,
        "integrated": merged,
        "source": "github_pr",
        "pr_number": pr_number,
        "pr_url": str(body.get("html_url") or ""),
        "state": str(body.get("state") or ""),
        "merged_at": body.get("merged_at"),
        "merge_commit_sha": str(body.get("merge_commit_sha") or ""),
        "pr_head_sha": str(head.get("sha") or ""),
    }


def _dirty_result(
    task: Dict[str, Any],
    *,
    evidence: Dict[str, Any],
    current_head: str,
) -> Dict[str, Any]:
    detail = dict(evidence)
    detail.update(
        {
            "integrated": False,
            "current_head": current_head,
            "workspace_dirty": True,
            "reason": "workspace_has_uncommitted_changes",
        }
    )
    return {
        "proceed": True,
        "status": "post_merge_changes",
        "evidence": detail,
        "task": task,
    }


def _mark_local_integration(
    task_id: str,
    local_state: Dict[str, Any],
    pr_state: Dict[str, Any],
    *,
    actor: str,
) -> Dict[str, Any]:
    evidence = dict(local_state)
    if pr_state:
        evidence.update(
            {
                "pr_number": pr_state.get("pr_number"),
                "pr_url": pr_state.get("pr_url"),
                "merged_at": pr_state.get("merged_at"),
                "pr_head_sha": pr_state.get("pr_head_sha"),
            }
        )
    stored = base._mark_integrated(task_id, evidence, actor=actor)
    return {"proceed": False, "status": "integrated", "evidence": evidence, "task": stored}


def _reconcile_local(
    task_id: str,
    task: Dict[str, Any],
    *,
    git_token_value: Optional[str],
    actor: str,
    pr_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current_head = _workspace_head(task_id, task)
    dirty = _workspace_dirty(task_id)
    evidence = dict(pr_state or {})
    if dirty is True:
        return _dirty_result(task, evidence=evidence, current_head=current_head)

    local_state = base._local_integration_state(task, git_token_value=git_token_value)
    if local_state.get("integrated") and dirty is False:
        return _mark_local_integration(
            task_id,
            local_state,
            pr_state or {},
            actor=actor,
        )

    evidence.update(
        {
            "integrated": False,
            "current_head": current_head,
            "workspace_dirty": dirty,
            "workspace_state": local_state,
        }
    )
    return {
        "proceed": True,
        "status": "not_integrated" if local_state.get("known") and dirty is False else "reconciliation_unknown",
        "evidence": evidence,
        "task": task,
    }


def reconcile_task_before_run(
    task_id: str,
    *,
    git_token_value: Optional[str] = None,
    actor: str = "coding-agent",
) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    pr_number, pr_url = base._pull_request_reference(task)
    if not base._has_reconcilable_history(task, pr_number=pr_number):
        return {"proceed": True, "status": "not_applicable", "task": task}
    if pr_number <= 0:
        return _reconcile_local(
            task_id,
            task,
            git_token_value=git_token_value,
            actor=actor,
        )

    pr_state = _pull_request_state(
        task,
        pr_number=pr_number,
        git_token_value=git_token_value,
    )
    if pr_url and not pr_state.get("pr_url"):
        pr_state["pr_url"] = pr_url

    if not pr_state.get("known"):
        return _reconcile_local(
            task_id,
            task,
            git_token_value=git_token_value,
            actor=actor,
            pr_state=pr_state,
        )
    if not pr_state.get("integrated"):
        if str(pr_state.get("state") or "").strip().lower() == "open":
            return {
                "proceed": True,
                "status": "open_pull_request",
                "evidence": pr_state,
                "task": task,
            }
        return _reconcile_local(
            task_id,
            task,
            git_token_value=git_token_value,
            actor=actor,
            pr_state=pr_state,
        )

    current_head = _workspace_head(task_id, task)
    dirty = _workspace_dirty(task_id)
    if dirty is True:
        return _dirty_result(task, evidence=pr_state, current_head=current_head)

    pr_head = str(pr_state.get("pr_head_sha") or "").strip()
    if current_head and pr_head and current_head == pr_head and dirty is False:
        stored = base._mark_integrated(task_id, pr_state, actor=actor)
        return {"proceed": False, "status": "integrated", "evidence": pr_state, "task": stored}

    local_state = base._local_integration_state(task, git_token_value=git_token_value)
    if local_state.get("integrated") and dirty is False:
        return _mark_local_integration(task_id, local_state, pr_state, actor=actor)

    evidence = dict(pr_state)
    evidence.update(
        {
            "integrated": False,
            "current_head": current_head,
            "workspace_dirty": dirty,
            "workspace_state": local_state,
        }
    )
    if current_head and pr_head and current_head != pr_head:
        evidence["reason"] = "workspace_head_advanced_after_merged_pull_request"
        return {
            "proceed": True,
            "status": "post_merge_changes",
            "evidence": evidence,
            "task": task,
        }
    if local_state.get("known") and dirty is False:
        evidence["reason"] = "workspace_changes_not_integrated"
        return {
            "proceed": True,
            "status": "post_merge_changes",
            "evidence": evidence,
            "task": task,
        }

    evidence["reason"] = "merged_pull_request_but_workspace_relationship_unknown"
    return {
        "proceed": True,
        "status": "reconciliation_unknown",
        "evidence": evidence,
        "task": task,
    }


async def reconcile_before_run(
    task_id: str,
    *,
    git_token_value: Optional[str] = None,
    actor: str = "coding-agent",
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        reconcile_task_before_run,
        task_id,
        git_token_value=git_token_value,
        actor=actor,
    )
