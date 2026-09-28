from __future__ import annotations

import asyncio
import os
import secrets
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


def _retryable_git_operation(
    argv: Sequence[str],
    *,
    cwd: Path,
    workspace_root: Path,
) -> bool:
    subcommand = _git_subcommand(argv)
    del cwd, workspace_root
    # Clone can leave a partial destination. Never delete or retry that path in
    # this generic wrapper; failed workspace initialization has a separate,
    # descriptor-anchored recovery path.
    return subcommand in _RETRYABLE_GIT_SUBCOMMANDS


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
    history: list[Dict[str, Any]] = []

    for index in range(max_attempts):
        result = original(argv, cwd=cwd, **kwargs)
        kind = classify_transient_text(
            f"{result.get('stderr') or ''}\n{result.get('stdout') or ''}"
        )
        history.append(_retry_history_entry(result, attempt=index + 1, kind=kind))
        if result.get("ok") or not retryable_operation or not kind or index + 1 >= max_attempts:
            return _with_retry_metadata(result, history)

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
    raw_repo_path = str(task.get("repo_path") or "").strip()
    raw_workspace_path = str(task.get("workspace_path") or "").strip()
    if not raw_repo_path or not raw_workspace_path:
        raise HTTPException(
            status_code=409,
            detail="coding workspace paths are not safe to reinitialize",
        )

    # Keep the controller paths lexical. Recovery later opens them with
    # O_NOFOLLOW and performs clone operations through that held directory fd.
    workspace_root = Path(os.path.abspath(str(cw.workspace_root())))
    workspace_path = Path(os.path.abspath(raw_workspace_path))
    repo_path = Path(os.path.abspath(raw_repo_path))
    expected_workspace_path = workspace_root.joinpath(task_id)
    expected_repo_path = expected_workspace_path.joinpath("repo")
    if (
        workspace_path != expected_workspace_path
        or repo_path != expected_repo_path
        or workspace_path.name != task_id
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "coding workspace paths do not match the controller-owned "
                "<task>/repo layout"
            ),
        )
    if workspace_path.is_symlink() or repo_path.is_symlink():
        raise HTTPException(
            status_code=409,
            detail="coding workspace paths may not be symbolic links",
        )
    try:
        resolved_root = workspace_root.resolve()
        resolved_workspace = workspace_path.resolve()
        relative_workspace = resolved_workspace.relative_to(resolved_root)
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail="coding workspace paths are not safe to reinitialize",
        ) from exc
    if relative_workspace.parts != (task_id,):
        raise HTTPException(
            status_code=409,
            detail=(
                "coding workspace paths do not match the controller-owned "
                "<task>/repo layout"
            ),
        )

    status = str(task.get("status") or "").strip().lower()
    failure_kind = _latest_transient_clone_failure(task)

    if status == "ready" and _valid_git_repo(repo_path):
        return task
    if status != "error":
        raise HTTPException(
            status_code=409,
            detail=f"coding workspace is not ready for an agent run (status={status or 'unknown'})",
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

    if os.name != "posix" or not Path("/proc/self/fd").is_dir():
        raise HTTPException(
            status_code=409,
            detail=(
                "coding workspace initialization recovery requires anchored "
                "POSIX directory descriptors"
            ),
        )
    open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise HTTPException(
            status_code=409,
            detail="coding workspace initialization recovery requires O_NOFOLLOW",
        )

    root_fd = -1
    workspace_fd = -1
    repo_fd = -1
    partial_repo_quarantine = ""
    try:
        root_fd = os.open(workspace_root, open_flags | nofollow)
        try:
            workspace_fd = os.open(
                task_id,
                open_flags | nofollow,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            os.mkdir(task_id, mode=0o700, dir_fd=root_fd)
            workspace_fd = os.open(
                task_id,
                open_flags | nofollow,
                dir_fd=root_fd,
            )

        workspace_stat = os.fstat(workspace_fd)

        def workspace_path_is_stable() -> bool:
            try:
                return os.path.samestat(
                    workspace_stat,
                    os.stat(workspace_path, follow_symlinks=False),
                )
            except OSError:
                return False

        if not workspace_path_is_stable():
            raise HTTPException(
                status_code=409,
                detail="coding workspace path changed during initialization recovery",
            )
        try:
            os.stat("repo", dir_fd=workspace_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            quarantine_prefix = "repo.partial-"
            existing_quarantines = [
                name
                for name in os.listdir(workspace_fd)
                if str(name).startswith(quarantine_prefix)
            ]
            if len(existing_quarantines) >= 3:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "coding workspace has reached the preserved partial "
                        "repository limit; inspect it before retrying"
                    ),
                )
            quarantine_name = ""
            for _ in range(8):
                candidate = f"{quarantine_prefix}{secrets.token_hex(6)}"
                try:
                    os.mkdir(candidate, mode=0o700, dir_fd=workspace_fd)
                except FileExistsError:
                    continue
                quarantine_name = candidate
                break
            if not quarantine_name:
                raise HTTPException(
                    status_code=409,
                    detail="could not reserve a partial repository quarantine",
                )
            quarantine_fd = os.open(
                quarantine_name,
                open_flags | nofollow,
                dir_fd=workspace_fd,
            )
            try:
                os.rename(
                    "repo",
                    "repo",
                    src_dir_fd=workspace_fd,
                    dst_dir_fd=quarantine_fd,
                )
            finally:
                os.close(quarantine_fd)
            partial_repo_quarantine = f"{quarantine_name}/repo"
            task["initialization_recovery"] = {
                "recovered": False,
                "reason": failure_kind,
                "attempted_at": time.time(),
                "partial_repo_quarantine": partial_repo_quarantine,
            }
            cw.save_task(task)
            if not workspace_path_is_stable():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "coding workspace path changed during initialization "
                        "recovery"
                    ),
                )

        anchored_workspace = Path(f"/proc/self/fd/{workspace_fd}")
        clone_target = anchored_workspace.joinpath("repo")
        run_process = getattr(
            cw,
            "_network_resilience_original_run_process",
            cw._run_process,
        )
        repo_url = str(task.get("repo_url") or "").strip()
        base = str(task.get("base_branch") or "main").strip() or "main"
        branch = str(task.get("branch_name") or "").strip()
        clone_result = run_process(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                base,
                repo_url,
                str(clone_target),
            ],
            cwd=anchored_workspace,
            timeout_sec=max(cw.command_timeout_sec(), 300.0),
            use_git_credentials=True,
            git_token_value=git_token_value,
            pass_fds=(workspace_fd,),
        )
        cw._append_command(task, clone_result, label="clone-retry")
        if not clone_result.get("ok"):
            task["status"] = "error"
            task["error"] = "git clone failed after initialization retry"
            task["initialization_recovery"] = {
                "recovered": False,
                "reason": failure_kind,
                "attempted_at": time.time(),
                "network_retry_attempts": int(
                    clone_result.get("network_retry_attempts") or 1
                ),
            }
            if partial_repo_quarantine:
                task["initialization_recovery"]["partial_repo_quarantine"] = (
                    partial_repo_quarantine
                )
            cw.save_task(task)
            raise HTTPException(status_code=503, detail=task["error"])

        if not workspace_path_is_stable():
            raise HTTPException(
                status_code=409,
                detail="coding workspace path changed during initialization recovery",
            )

        repo_fd = os.open(
            "repo",
            open_flags | nofollow,
            dir_fd=workspace_fd,
        )
        repo_stat = os.fstat(repo_fd)

        def repo_path_is_stable() -> bool:
            try:
                return os.path.samestat(
                    repo_stat,
                    os.stat(
                        "repo",
                        dir_fd=workspace_fd,
                        follow_symlinks=False,
                    ),
                )
            except OSError:
                return False

        if not repo_path_is_stable():
            raise HTTPException(
                status_code=409,
                detail="coding repository path changed during initialization recovery",
            )
        anchored_repo = Path(f"/proc/self/fd/{repo_fd}")

        if branch and branch != base:
            switch_result = run_process(
                ["git", "switch", "-c", branch],
                cwd=anchored_repo,
                use_git_credentials=False,
                pass_fds=(workspace_fd, repo_fd),
            )
            if not switch_result.get("ok"):
                switch_result = run_process(
                    ["git", "checkout", "-b", branch],
                    cwd=anchored_repo,
                    use_git_credentials=False,
                    pass_fds=(workspace_fd, repo_fd),
                )
            cw._append_command(task, switch_result, label="branch-retry")
            if not switch_result.get("ok"):
                task["status"] = "error"
                task["error"] = (
                    "branch creation failed after initialization retry"
                )
                cw.save_task(task)
                raise HTTPException(status_code=409, detail=task["error"])

        if not workspace_path_is_stable() or not repo_path_is_stable():
            raise HTTPException(
                status_code=409,
                detail=(
                    "coding workspace or repository path changed during "
                    "initialization recovery"
                ),
            )
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "coding workspace initialization recovery could not securely "
                f"open its controller-owned path ({type(exc).__name__})"
            ),
        ) from exc
    finally:
        if repo_fd >= 0:
            os.close(repo_fd)
        if workspace_fd >= 0:
            os.close(workspace_fd)
        if root_fd >= 0:
            os.close(root_fd)

    task["status"] = "ready"
    task.pop("error", None)
    task["initialization_recovery"] = {
        "recovered": True,
        "reason": failure_kind,
        "attempted_at": time.time(),
        "network_retry_attempts": int(clone_result.get("network_retry_attempts") or 1),
        "workspace_identity": {
            "device": int(workspace_stat.st_dev),
            "inode": int(workspace_stat.st_ino),
        },
        "repo_identity": {
            "device": int(repo_stat.st_dev),
            "inode": int(repo_stat.st_ino),
        },
    }
    if partial_repo_quarantine:
        task["initialization_recovery"]["partial_repo_quarantine"] = (
            partial_repo_quarantine
        )
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
        status = str(task.get("status") or "").strip().lower()
        if (
            status == "error"
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
