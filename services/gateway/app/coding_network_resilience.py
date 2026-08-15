from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from fastapi import HTTPException


_RETRYABLE_GIT_SUBCOMMANDS = {"fetch", "ls-remote", "push"}
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_DNS_MARKERS = (
    "could not resolve host",
    "could not resolve hostname",
    "could not resolve proxy",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "getaddrinfo failed",
)
_TIMEOUT_MARKERS = (
    "connection timed out",
    "operation timed out",
)
_CONNECT_MARKERS = (
    "failed to connect",
    "connection reset by peer",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "connection closed by remote host",
    "recv failure",
    "send failure",
    "remote end hung up unexpectedly",
    "gnutls recv error",
    "tls connection was non-properly terminated",
    "openssl ssl_connect",
    "http/2 stream",
)
_SERVER_MARKERS = (
    "the requested url returned error: 500",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
    "remote: internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.environ.get(name, default)).strip())
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


def retry_attempts() -> int:
    """Total attempts for retry-safe coding network operations."""
    return _env_int("CODING_GIT_RETRY_ATTEMPTS", 4, minimum=1, maximum=8)


def retry_base_delay_sec() -> float:
    return _env_float("CODING_GIT_RETRY_BASE_SEC", 1.0, minimum=0.0, maximum=30.0)


def _retry_delay(attempt_index: int, base_delay: float) -> float:
    # attempt_index is zero-based and describes the attempt that just failed.
    return min(30.0, max(0.0, base_delay) * (2**attempt_index))


def classify_transient_text(value: str) -> str:
    text = str(value or "").lower()
    if any(marker in text for marker in _DNS_MARKERS):
        return "dns"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout"
    if any(marker in text for marker in _CONNECT_MARKERS):
        return "connect"
    if any(marker in text for marker in _SERVER_MARKERS):
        return "server"
    return ""


def _git_subcommand(argv: Sequence[str]) -> str:
    if not argv or Path(str(argv[0])).name.lower() != "git":
        return ""
    skip_next = False
    for raw in list(argv)[1:]:
        token = str(raw)
        if skip_next:
            skip_next = False
            continue
        if token in {"-C", "-c"}:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token.lower()
    return ""


def _internal_clone_destination(
    argv: Sequence[str],
    *,
    cwd: Path,
    workspace_root: Path,
) -> Optional[Path]:
    if _git_subcommand(argv) != "clone" or len(argv) < 3:
        return None
    raw = str(argv[-1] or "").strip()
    if not raw or raw.startswith("-"):
        return None
    destination = Path(raw)
    if not destination.is_absolute():
        destination = Path(cwd).joinpath(destination)
    try:
        destination = destination.resolve()
        root = Path(workspace_root).resolve()
        relative = destination.relative_to(root)
    except Exception:
        return None
    if len(relative.parts) != 2:
        return None
    if not re.fullmatch(r"code_[a-f0-9]{12}", relative.parts[0]):
        return None
    if relative.parts[1] != "repo":
        return None
    return destination


def _retryable_git_operation(
    argv: Sequence[str],
    *,
    cwd: Path,
    workspace_root: Path,
) -> bool:
    subcommand = _git_subcommand(argv)
    if subcommand in _RETRYABLE_GIT_SUBCOMMANDS:
        return True
    if subcommand == "clone":
        return _internal_clone_destination(
            argv,
            cwd=Path(cwd),
            workspace_root=Path(workspace_root),
        ) is not None
    return False


def _retry_history_entry(result: Dict[str, Any], *, attempt: int, kind: str) -> Dict[str, Any]:
    return {
        "attempt": attempt,
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "kind": str(kind or ""),
        "duration_ms": result.get("duration_ms"),
        "stderr_tail": str(result.get("stderr") or "")[-1200:],
    }


def _with_retry_metadata(result: Dict[str, Any], history: list[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(result)
    out["network_retry_attempts"] = len(history)
    out["network_retry_count"] = max(0, len(history) - 1)
    out["network_retry_recovered"] = bool(out.get("ok")) and len(history) > 1
    out["network_retry_history"] = history
    out["network_error_kind"] = next(
        (str(item.get("kind") or "") for item in reversed(history) if item.get("kind")),
        "",
    )
    return out


def run_process_with_retry(
    original: Callable[..., Dict[str, Any]],
    argv: Sequence[str],
    *,
    cwd: Path,
    workspace_root: Path,
    sleep_fn: Callable[[float], None] = time.sleep,
    attempts: Optional[int] = None,
    base_delay_sec: Optional[float] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    max_attempts = retry_attempts() if attempts is None else max(1, int(attempts))
    base_delay = retry_base_delay_sec() if base_delay_sec is None else max(0.0, float(base_delay_sec))
    retryable_operation = _retryable_git_operation(
        argv,
        cwd=Path(cwd),
        workspace_root=Path(workspace_root),
    )
    clone_destination = _internal_clone_destination(
        argv,
        cwd=Path(cwd),
        workspace_root=Path(workspace_root),
    )
    history: list[Dict[str, Any]] = []

    for index in range(max_attempts):
        result = original(argv, cwd=cwd, **kwargs)
        kind = classify_transient_text(
            f"{result.get('stderr') or ''}\n{result.get('stdout') or ''}"
        )
        history.append(_retry_history_entry(result, attempt=index + 1, kind=kind))
        if result.get("ok") or not retryable_operation or not kind or index + 1 >= max_attempts:
            return _with_retry_metadata(result, history)

        # git clone can leave a partial destination. Only remove the controller-owned
        # code_<id>/repo target created during workspace initialization.
        if clone_destination is not None and clone_destination.exists():
            try:
                shutil.rmtree(clone_destination)
            except Exception as exc:
                failed = dict(result)
                failed["stderr"] = (
                    f"{failed.get('stderr') or ''}\n"
                    f"network retry aborted: failed to remove partial clone destination: "
                    f"{type(exc).__name__}: {exc}"
                ).strip()
                history[-1]["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                return _with_retry_metadata(failed, history)

        delay = _retry_delay(index, base_delay)
        if delay > 0:
            sleep_fn(delay)

    return _with_retry_metadata(result, history)  # pragma: no cover - loop always returns


def _http_result_kind(result: Dict[str, Any]) -> str:
    kind = classify_transient_text(str(result.get("error") or ""))
    if kind:
        return kind
    try:
        status = int(result.get("status") or 0)
    except Exception:
        status = 0
    if status in _RETRYABLE_HTTP_STATUSES:
        return "http_status"
    return ""


def _http_retry_allowed(method: str, result: Dict[str, Any]) -> bool:
    kind = _http_result_kind(result)
    if not kind:
        return False
    normalized = str(method or "GET").upper()
    if normalized in {"GET", "HEAD"}:
        return True
    # DNS resolution fails before an HTTP request can be sent, so retrying a
    # non-idempotent GitHub write is safe for this one failure class. Other
    # write failures remain fail-fast to avoid duplicate repo/PR creation.
    return kind == "dns"


def github_api_with_retry(
    original: Callable[..., Dict[str, Any]],
    method: str,
    path: str,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    attempts: Optional[int] = None,
    base_delay_sec: Optional[float] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    max_attempts = retry_attempts() if attempts is None else max(1, int(attempts))
    base_delay = retry_base_delay_sec() if base_delay_sec is None else max(0.0, float(base_delay_sec))
    history: list[Dict[str, Any]] = []
    result: Dict[str, Any] = {}
    for index in range(max_attempts):
        result = original(method, path, **kwargs)
        kind = _http_result_kind(result)
        history.append(
            {
                "attempt": index + 1,
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
                "kind": kind,
                "error": str(result.get("error") or "")[-1200:],
            }
        )
        if result.get("ok") or not _http_retry_allowed(method, result) or index + 1 >= max_attempts:
            out = dict(result)
            out["network_retry_attempts"] = len(history)
            out["network_retry_count"] = max(0, len(history) - 1)
            out["network_retry_recovered"] = bool(out.get("ok")) and len(history) > 1
            out["network_retry_history"] = history
            return out
        delay = _retry_delay(index, base_delay)
        if delay > 0:
            sleep_fn(delay)
    return result  # pragma: no cover


def github_pr_create_with_dns_retry(
    original: Callable[..., Dict[str, Any]],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    attempts: Optional[int] = None,
    base_delay_sec: Optional[float] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    max_attempts = retry_attempts() if attempts is None else max(1, int(attempts))
    base_delay = retry_base_delay_sec() if base_delay_sec is None else max(0.0, float(base_delay_sec))
    history: list[Dict[str, Any]] = []
    result: Dict[str, Any] = {}
    for index in range(max_attempts):
        result = original(**kwargs)
        kind = _http_result_kind(result)
        history.append(
            {
                "attempt": index + 1,
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
                "kind": kind,
                "error": str(result.get("error") or "")[-1200:],
            }
        )
        # PR creation is not idempotent. Retry only a DNS failure, which occurs
        # before api.github.com can receive the POST.
        if result.get("ok") or kind != "dns" or index + 1 >= max_attempts:
            out = dict(result)
            out["network_retry_attempts"] = len(history)
            out["network_retry_count"] = max(0, len(history) - 1)
            out["network_retry_recovered"] = bool(out.get("ok")) and len(history) > 1
            out["network_retry_history"] = history
            return out
        delay = _retry_delay(index, base_delay)
        if delay > 0:
            sleep_fn(delay)
    return result  # pragma: no cover


def _latest_transient_clone_failure(task: Dict[str, Any]) -> str:
    commands = task.get("commands") if isinstance(task.get("commands"), list) else []
    for item in reversed(commands):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        if label not in {"clone", "clone-retry", "git-clone-base"}:
            continue
        if bool(item.get("ok")):
            return ""
        kind = classify_transient_text(
            f"{item.get('stderr_tail') or ''}\n{item.get('stdout_tail') or ''}"
        )
        return kind
    return ""


def _valid_git_repo(path: Path) -> bool:
    return path.is_dir() and path.joinpath(".git").exists()


def retry_failed_initialization(
    cw: Any,
    task_id: str,
    *,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    task = cw.load_task(task_id)
    repo_path = Path(str(task.get("repo_path") or "")).resolve()
    workspace_path = Path(str(task.get("workspace_path") or "")).resolve()
    status = str(task.get("status") or "").strip().lower()
    failure_kind = _latest_transient_clone_failure(task)

    if status == "ready" and _valid_git_repo(repo_path):
        return task
    if status != "error":
        raise HTTPException(
            status_code=409,
            detail=f"coding workspace is not ready for an agent run (status={status or 'unknown'})",
        )
    if _valid_git_repo(repo_path):
        raise HTTPException(
            status_code=409,
            detail=(
                "coding workspace now contains valid Git metadata; automatic reclone is "
                "disabled to preserve repaired or modified repository state"
            ),
        )
    if str(task.get("kind") or "") == "model_integration":
        raise HTTPException(
            status_code=409,
            detail=(
                "model integration workspace initialization is incomplete; create a fresh "
                "workspace so repository provisioning and scaffolding can run as one transaction"
            ),
        )
    if not failure_kind:
        raise HTTPException(
            status_code=409,
            detail=(
                "coding workspace initialization failed for a non-transient reason; "
                "inspect the recorded clone/branch error before retrying"
            ),
        )

    workspace_root = Path(cw.workspace_root()).resolve()
    expected_repo_path = workspace_path.joinpath("repo").resolve()
    try:
        relative_workspace = workspace_path.relative_to(workspace_root)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="coding workspace paths are not safe to reinitialize") from exc
    if (
        workspace_path.name != task_id
        or relative_workspace.parts != (task_id,)
        or repo_path != expected_repo_path
    ):
        raise HTTPException(
            status_code=409,
            detail="coding workspace paths do not match the controller-owned <task>/repo layout",
        )

    # A recorded transient clone failure never established a usable workspace.
    # This exact controller-owned repo target is the only path automatic recovery
    # is permitted to delete.
    if repo_path.exists():
        try:
            shutil.rmtree(repo_path)
        except Exception as exc:
            task["initialization_recovery"] = {
                "recovered": False,
                "reason": failure_kind,
                "attempted_at": time.time(),
                "cleanup_error": f"{type(exc).__name__}: {exc}",
            }
            cw.save_task(task)
            raise HTTPException(
                status_code=409,
                detail=(
                    "coding workspace initialization retry could not safely remove the partial "
                    f"clone: {type(exc).__name__}: {exc}"
                ),
            ) from exc
    workspace_path.mkdir(parents=True, exist_ok=True)

    repo_url = str(task.get("repo_url") or "").strip()
    base = str(task.get("base_branch") or "main").strip() or "main"
    branch = str(task.get("branch_name") or "").strip()
    clone_result = cw._run_process(
        ["git", "clone", "--depth", "1", "--branch", base, repo_url, str(repo_path)],
        cwd=workspace_path,
        timeout_sec=max(cw.command_timeout_sec(), 300.0),
        use_git_credentials=True,
        git_token_value=git_token_value,
    )
    cw._append_command(task, clone_result, label="clone-retry")
    if not clone_result.get("ok"):
        task["status"] = "error"
        task["error"] = "git clone failed after initialization retry"
        task["initialization_recovery"] = {
            "recovered": False,
            "reason": failure_kind,
            "attempted_at": time.time(),
            "network_retry_attempts": int(clone_result.get("network_retry_attempts") or 1),
        }
        cw.save_task(task)
        raise HTTPException(status_code=503, detail=task["error"])

    if branch and branch != base:
        switch_result = cw._run_process(
            ["git", "switch", "-c", branch],
            cwd=repo_path,
            use_git_credentials=False,
        )
        if not switch_result.get("ok"):
            switch_result = cw._run_process(
                ["git", "checkout", "-b", branch],
                cwd=repo_path,
                use_git_credentials=False,
            )
        cw._append_command(task, switch_result, label="branch-retry")
        if not switch_result.get("ok"):
            task["status"] = "error"
            task["error"] = "branch creation failed after initialization retry"
            cw.save_task(task)
            raise HTTPException(status_code=409, detail=task["error"])

    task["status"] = "ready"
    task.pop("error", None)
    task["initialization_recovery"] = {
        "recovered": True,
        "reason": failure_kind,
        "attempted_at": time.time(),
        "network_retry_attempts": int(clone_result.get("network_retry_attempts") or 1),
    }
    cw.save_task(task)
    return task


def install(cw: Any, guarded_agent: Any = None) -> None:
    """Install bounded network resilience into the guarded Coding Workspace runtime."""
    if not bool(getattr(cw, "_coding_network_resilience_installed", False)):
        original_run_process = cw._run_process
        original_command_summary = cw._command_summary
        original_github_api_request = cw._github_api_request
        original_create_github_pr_api = cw._create_github_pr_api

        @wraps(original_run_process)
        def resilient_run_process(argv: Sequence[str], *, cwd: Path, **kwargs: Any) -> Dict[str, Any]:
            return run_process_with_retry(
                original_run_process,
                argv,
                cwd=cwd,
                workspace_root=cw.workspace_root(),
                **kwargs,
            )

        @wraps(original_command_summary)
        def resilient_command_summary(result: Dict[str, Any], *, label: str) -> Dict[str, Any]:
            summary = original_command_summary(result, label=label)
            for key in (
                "network_retry_attempts",
                "network_retry_count",
                "network_retry_recovered",
                "network_retry_history",
                "network_error_kind",
            ):
                if key in result:
                    summary[key] = result[key]
            return summary

        @wraps(original_github_api_request)
        def resilient_github_api_request(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
            return github_api_with_retry(
                original_github_api_request,
                method,
                path,
                **kwargs,
            )

        @wraps(original_create_github_pr_api)
        def resilient_create_github_pr_api(**kwargs: Any) -> Dict[str, Any]:
            return github_pr_create_with_dns_retry(
                original_create_github_pr_api,
                **kwargs,
            )

        cw._network_resilience_original_run_process = original_run_process
        cw._network_resilience_original_command_summary = original_command_summary
        cw._network_resilience_original_github_api_request = original_github_api_request
        cw._network_resilience_original_create_github_pr_api = original_create_github_pr_api
        cw._run_process = resilient_run_process
        cw._command_summary = resilient_command_summary
        cw._github_api_request = resilient_github_api_request
        cw._create_github_pr_api = resilient_create_github_pr_api
        cw._coding_network_resilience_installed = True

    if guarded_agent is None or bool(getattr(guarded_agent, "_coding_network_resilience_installed", False)):
        return

    original_start_agent_run = guarded_agent.start_agent_run

    @wraps(original_start_agent_run)
    async def resilient_start_agent_run(task_id: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        task = await asyncio.to_thread(cw.load_task, task_id)
        repo_path = Path(str(task.get("repo_path") or "")).resolve()
        status = str(task.get("status") or "").strip().lower()
        if (
            status == "error"
            and not _valid_git_repo(repo_path)
            and bool(_latest_transient_clone_failure(task))
        ):
            await asyncio.to_thread(
                retry_failed_initialization,
                cw,
                task_id,
                git_token_value=kwargs.get("git_token_value"),
            )
        return await original_start_agent_run(task_id, *args, **kwargs)

    guarded_agent._network_resilience_original_start_agent_run = original_start_agent_run
    guarded_agent.start_agent_run = resilient_start_agent_run
    guarded_agent._coding_network_resilience_installed = True
