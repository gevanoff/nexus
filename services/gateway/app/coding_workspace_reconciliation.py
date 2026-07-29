from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app import coding_workspace as cw


_PR_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/pull/(\d+)", re.IGNORECASE)
_TERMINAL_AGENT_STATUSES = {"completed", "failed", "interrupted", "paused", "stopped"}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pull_request_reference(task: Dict[str, Any]) -> Tuple[int, str]:
    candidates = []
    terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
    candidates.append(terminal)
    runs = task.get("agent_runs") if isinstance(task.get("agent_runs"), list) else []
    candidates.extend(item for item in reversed(runs) if isinstance(item, dict))
    for item in candidates:
        number = _as_int(item.get("pr_number"))
        url = str(item.get("pr_url") or item.get("pull_request_url") or "").strip()
        if number > 0:
            return number, url
        match = _PR_URL_RE.search(url)
        if match:
            return int(match.group(1)), url
    output = str(task.get("last_pr_output") or "").strip()
    match = _PR_URL_RE.search(output)
    if match:
        return int(match.group(1)), match.group(0)
    return 0, ""


def _has_reconcilable_history(task: Dict[str, Any], *, pr_number: int) -> bool:
    if pr_number > 0:
        return True
    agent_status = str(task.get("agent_status") or "").strip().lower()
    if agent_status not in _TERMINAL_AGENT_STATUSES:
        return False
    candidate = str(task.get("last_commit") or task.get("last_checkpoint_commit") or "").strip()
    start_head = str(task.get("agent_start_head") or "").strip()
    if candidate and (not start_head or candidate != start_head):
        return True
    return bool(task.get("last_pushed_at") or task.get("last_pr_at"))


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


def _run_git(
    repo: Path,
    argv: list[str],
    *,
    git_token_value: Optional[str] = None,
    timeout_sec: float = 60.0,
) -> Dict[str, Any]:
    return cw._run_process(
        ["git", *argv],
        cwd=repo,
        timeout_sec=timeout_sec,
        use_git_credentials=True,
        git_token_value=git_token_value,
    )


def _local_integration_state(
    task: Dict[str, Any],
    *,
    git_token_value: Optional[str],
) -> Dict[str, Any]:
    raw_repo = str(task.get("repo_path") or "").strip()
    if not raw_repo:
        return {"known": False, "integrated": False, "source": "git_ancestry", "error": "repo path missing"}
    repo = Path(raw_repo).resolve()
    if not repo.joinpath(".git").exists():
        return {"known": False, "integrated": False, "source": "git_ancestry", "error": "git metadata missing"}

    base = str(task.get("base_branch") or "main").strip() or "main"
    fetch = _run_git(
        repo,
        ["fetch", "--quiet", "origin", f"{base}:refs/remotes/origin/{base}"],
        git_token_value=git_token_value,
        timeout_sec=180.0,
    )
    remote_ref = f"refs/remotes/origin/{base}"
    verify = _run_git(repo, ["rev-parse", "--verify", remote_ref], git_token_value=git_token_value)
    if not verify.get("ok"):
        local_ref = _run_git(repo, ["rev-parse", "--verify", base], git_token_value=git_token_value)
        if not local_ref.get("ok"):
            return {
                "known": False,
                "integrated": False,
                "source": "git_ancestry",
                "error": str(fetch.get("stderr") or fetch.get("error") or "base ref unavailable")[:1000],
            }
        remote_ref = base

    head = _run_git(repo, ["rev-parse", "HEAD"], git_token_value=git_token_value)
    head_sha = str(head.get("stdout") or "").strip()
    if not head.get("ok") or not head_sha:
        return {"known": False, "integrated": False, "source": "git_ancestry", "error": "workspace HEAD unavailable"}

    ancestor = _run_git(
        repo,
        ["merge-base", "--is-ancestor", head_sha, remote_ref],
        git_token_value=git_token_value,
    )
    if int(ancestor.get("returncode") or 0) == 0 and ancestor.get("ok"):
        return {
            "known": True,
            "integrated": True,
            "source": "git_ancestry",
            "head": head_sha,
            "base_ref": remote_ref,
            "fetch_ok": bool(fetch.get("ok")),
        }

    cherry = _run_git(repo, ["cherry", remote_ref, head_sha], git_token_value=git_token_value)
    if cherry.get("ok"):
        lines = [line.strip() for line in str(cherry.get("stdout") or "").splitlines() if line.strip()]
        if lines and all(line.startswith("-") for line in lines):
            return {
                "known": True,
                "integrated": True,
                "source": "git_patch_equivalence",
                "head": head_sha,
                "base_ref": remote_ref,
                "equivalent_commits": len(lines),
                "fetch_ok": bool(fetch.get("ok")),
            }
        return {
            "known": True,
            "integrated": False,
            "source": "git_patch_equivalence",
            "head": head_sha,
            "base_ref": remote_ref,
            "ahead_commits": len(lines),
            "fetch_ok": bool(fetch.get("ok")),
        }
    return {
        "known": True,
        "integrated": False,
        "source": "git_ancestry",
        "head": head_sha,
        "base_ref": remote_ref,
        "fetch_ok": bool(fetch.get("ok")),
    }


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


def _mark_integrated(task_id: str, evidence: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    now = time.time()
    source = str(evidence.get("source") or "upstream")
    pr_number = _as_int(evidence.get("pr_number"))
    detail = f" PR #{pr_number}" if pr_number > 0 else ""
    summary = (
        f"Coding workspace work is already integrated upstream via {source}{detail}. "
        "This mission will not be restarted; create a follow-up workspace from the current base branch for new work."
    )

    def apply(task: Dict[str, Any]) -> None:
        task.update(
            {
                "agent_status": "completed",
                "agent_stop_requested": False,
                "agent_pause_requested": False,
                "agent_stop_reason_code": "work_already_integrated",
                "agent_summary": summary,
                "agent_error": "",
                "agent_finished_at": now,
                "agent_last_event_at": int(now),
                "integration_reconciliation": dict(evidence),
            }
        )
        terminal = task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {}
        terminal = dict(terminal)
        terminal.update(
            {
                "stop_reason_code": "work_already_integrated",
                "reconciliation": dict(evidence),
                "summary": summary,
            }
        )
        task["terminal_result"] = terminal
        events = task.get("agent_events") if isinstance(task.get("agent_events"), list) else []
        events.append(
            {
                "type": "work_already_integrated",
                "ts": int(now),
                "reason_code": "work_already_integrated",
                "summary": summary,
                "actor": actor,
                "evidence": dict(evidence),
            }
        )
        task["agent_events"] = events[-1000:]

    return cw.mutate_task(task_id, apply)


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
    stored = _mark_integrated(task_id, evidence, actor=actor)
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

    local_state = _local_integration_state(task, git_token_value=git_token_value)
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
    pr_number, pr_url = _pull_request_reference(task)
    if not _has_reconcilable_history(task, pr_number=pr_number):
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
        stored = _mark_integrated(task_id, pr_state, actor=actor)
        return {"proceed": False, "status": "integrated", "evidence": pr_state, "task": stored}

    local_state = _local_integration_state(task, git_token_value=git_token_value)
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
