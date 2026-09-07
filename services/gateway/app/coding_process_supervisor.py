from __future__ import annotations

import ctypes
import os
import pwd
import resource
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Sequence


_CONTAINMENT_ERROR_EXIT = 125
_DESCENDANT_QUIET_SEC = 0.25
_PR_SET_CHILD_SUBREAPER = 36
_RESOURCE_ERROR_EXIT = 125
_LANDLOCK_READ_ACCESS = "execute,read-file,read-dir"
_LANDLOCK_WRITE_ACCESS = (
    "write-file,remove-dir,remove-file,make-char,make-dir,make-reg,make-sock,"
    "make-fifo,make-block,make-sym,refer,truncate"
)
_LANDLOCK_ACCESS = f"{_LANDLOCK_READ_ACCESS},{_LANDLOCK_WRITE_ACCESS}"
_LANDLOCK_READ_DIRECTORIES = (
    Path("/usr"),
    Path("/proc"),
    Path("/dev"),
    Path("/etc/ld.so.conf.d"),
    Path("/etc/ssl/certs"),
)
_LANDLOCK_READ_FILES = (
    Path("/etc/ld.so.cache"),
    Path("/etc/ld.so.conf"),
    Path("/etc/localtime"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/passwd"),
    Path("/etc/group"),
    Path("/etc/ca-certificates.conf"),
)


class _TerminationRequested(Exception):
    pass


def _request_termination(_signal_number: int, _frame: object) -> NoReturn:
    raise _TerminationRequested()


def _set_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _direct_children(pid: int, *, required: bool = False) -> set[int]:
    children: set[int] = set()
    try:
        tasks = os.scandir(f"/proc/{pid}/task")
    except OSError as exc:
        if required:
            raise RuntimeError("procfs child enumeration is unavailable") from exc
        return children
    observed_task = False
    with tasks:
        for task in tasks:
            if not task.name.isdigit():
                continue
            try:
                text = Path(task.path, "children").read_text(encoding="ascii")
            except (FileNotFoundError, ProcessLookupError):
                continue
            observed_task = True
            children.update(int(value) for value in text.split() if value.isdigit())
    if required and not observed_task:
        raise RuntimeError("procfs child enumeration is unavailable")
    return children


def _descendant_tree(roots: set[int]) -> set[int]:
    found: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(_direct_children(pid) - found)
    return found


def _required_limit(name: str) -> int:
    raw = os.environ.pop(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"missing validation resource limit: {name}") from exc
    if value <= 0:
        raise RuntimeError(f"invalid validation resource limit: {name}")
    return value


def _required_nonnegative_limit(name: str) -> int:
    raw = os.environ.pop(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"missing validation resource limit: {name}") from exc
    if value < 0:
        raise RuntimeError(f"invalid validation resource limit: {name}")
    return value


def _validation_identity(*, allow_current_user: bool = False) -> tuple[int, int]:
    if os.geteuid() != 0:
        if allow_current_user:
            return os.geteuid(), os.getegid()
        raise RuntimeError("validation containment requires a root supervisor")
    account = pwd.getpwnam("nobody")
    return int(account.pw_uid), int(account.pw_gid)


def _prepare_validation_tree(root: Path, *, uid: int, gid: int) -> None:
    root.chmod(0o711)
    for child_name in ("scratch", "workspace"):
        child = root.joinpath(child_name)
        if not child.is_dir() or child.is_symlink():
            raise RuntimeError("validation workspace tree is unavailable")
        if child_name == "scratch":
            child.chmod(0o700)
    directory_modes = [
        (directory_path, stat.S_IMODE(directory_path.stat().st_mode))
        for directory, _, _ in os.walk(root, topdown=True, followlinks=False)
        if (directory_path := Path(directory)) != root
    ]
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *files]:
            path = directory_path.joinpath(name)
            os.chown(path, uid, gid, follow_symlinks=False)
        if directory_path != root:
            os.chown(directory_path, uid, gid, follow_symlinks=False)
    # chown can clear setgid, so restore staged directory modes only after every
    # entry has been handed to the unprivileged validation identity.
    for directory_path, mode in reversed(directory_modes):
        directory_path.chmod(mode)


def _apply_child_limits(
    *,
    uid: int,
    gid: int,
    file_bytes: int,
    open_files: int,
    processes: int,
    memory_bytes: int,
    cgroup: Path | None = None,
) -> None:
    if cgroup is not None:
        _join_validation_cgroup(cgroup)
    for kind, requested in (
        (resource.RLIMIT_FSIZE, file_bytes),
        (resource.RLIMIT_NOFILE, open_files),
        (resource.RLIMIT_NPROC, processes),
        (resource.RLIMIT_AS, memory_bytes),
    ):
        _, current_hard = resource.getrlimit(kind)
        effective = (
            requested
            if current_hard == resource.RLIM_INFINITY
            else min(requested, current_hard)
        )
        if effective <= 0:
            raise RuntimeError(f"validation resource limit {kind} is unavailable")
        resource.setrlimit(kind, (effective, effective))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)


def _scratch_usage(
    root: Path,
    *,
    stop_after_bytes: int | None = None,
    stop_after_entries: int | None = None,
) -> tuple[int, int]:
    total_bytes = 0
    total_entries = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"could not inspect validation scratch: {exc}") from exc
        with entries:
            for entry in entries:
                total_entries += 1
                if stop_after_entries is not None and total_entries > stop_after_entries:
                    return total_bytes, total_entries
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_symlink():
                        total_bytes += len(os.fsencode(os.readlink(entry.path)))
                    elif entry.is_file(follow_symlinks=False):
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
                if stop_after_bytes is not None and total_bytes > stop_after_bytes:
                    return total_bytes, total_entries
    return total_bytes, total_entries


def _scratch_limit_error(
    root: Path,
    *,
    max_bytes: int,
    max_entries: int,
    baseline_bytes: int = 0,
    baseline_entries: int = 0,
) -> str:
    total_bytes, total_entries = _scratch_usage(
        root,
        stop_after_bytes=baseline_bytes + max_bytes,
        stop_after_entries=baseline_entries + max_entries,
    )
    if total_entries - baseline_entries > max_entries:
        return f"validation scratch exceeded {max_entries} entry limit"
    if total_bytes - baseline_bytes > max_bytes:
        return f"validation scratch exceeded {max_bytes} byte limit"
    return ""


def _write_cgroup_value(path: Path, value: str) -> None:
    path.write_text(f"{value}\n", encoding="ascii")


def _create_validation_cgroup(
    root: Path,
    *,
    memory_bytes: int,
    processes: int,
) -> Path:
    if not root.is_absolute() or not root.is_dir():
        raise RuntimeError("validation cgroup delegation is unavailable")
    try:
        enabled = set(root.joinpath("cgroup.subtree_control").read_text(encoding="ascii").split())
    except OSError as exc:
        raise RuntimeError("validation cgroup delegation is unavailable") from exc
    if not {"memory", "pids"}.issubset(enabled):
        raise RuntimeError("validation cgroup memory and pids controllers are unavailable")
    group = root.joinpath(f"nexus-validation-{os.getpid()}-{secrets.token_hex(6)}")
    try:
        group.mkdir(mode=0o700)
        _write_cgroup_value(group.joinpath("memory.max"), str(memory_bytes))
        _write_cgroup_value(group.joinpath("memory.swap.max"), "0")
        _write_cgroup_value(group.joinpath("memory.oom.group"), "1")
        _write_cgroup_value(group.joinpath("pids.max"), str(processes))
    except OSError as exc:
        try:
            group.rmdir()
        except OSError:
            pass
        raise RuntimeError("could not configure validation cgroup") from exc
    return group


def _join_validation_cgroup(group: Path) -> None:
    _write_cgroup_value(group.joinpath("cgroup.procs"), str(os.getpid()))


def _landlocked_argv(argv: Sequence[str], *, writable_root: Path) -> list[str]:
    setpriv = Path("/usr/bin/setpriv")
    if not setpriv.is_file():
        raise RuntimeError("validation Landlock launcher is unavailable")
    command = [
        str(setpriv),
        "--no-new-privs",
        "--landlock-access",
        f"fs:{_LANDLOCK_ACCESS}",
        "--landlock-rule",
        f"path-beneath:{_LANDLOCK_ACCESS}:{writable_root}",
    ]
    for path in _LANDLOCK_READ_DIRECTORIES:
        if path.is_dir():
            command.extend(
                (
                    "--landlock-rule",
                    f"path-beneath:{_LANDLOCK_READ_ACCESS}:{path}",
                )
            )
    for path in _LANDLOCK_READ_FILES:
        if path.is_file():
            command.extend(
                ("--landlock-rule", f"path-beneath:read-file:{path}")
            )
    command.extend((
        "--landlock-rule",
        "path-beneath:write-file:/dev/null",
        "--",
        *[str(item) for item in argv],
    ))
    return command


def _run_task_command(argv: Sequence[str]) -> int:
    """Run a workspace command until every descendant has been reaped."""
    _set_child_subreaper()
    _direct_children(os.getpid(), required=True)
    signal.signal(signal.SIGTERM, _request_termination)
    signal.signal(signal.SIGINT, _request_termination)
    process: subprocess.Popen[bytes] | None = None
    returncode = _CONTAINMENT_ERROR_EXIT
    cleanup_error: Exception | None = None
    try:
        process = subprocess.Popen(list(argv), start_new_session=True)
        returncode = _exit_code(process.wait())
    except _TerminationRequested:
        returncode = 124
    finally:
        if process is not None:
            try:
                _terminate_descendants(process.pid)
            except Exception as exc:  # pragma: no cover - exceptional kernel failure
                cleanup_error = exc
    if cleanup_error is not None:
        raise cleanup_error
    return returncode


def _event_counts(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
        return {key: int(value) for key, value in (line.split() for line in lines)}
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"could not read validation cgroup evidence: {path.name}") from exc


def _cgroup_limit_error(group: Path) -> str:
    memory = _event_counts(group.joinpath("memory.events"))
    if any(memory.get(name, 0) > 0 for name in ("max", "oom", "oom_kill", "oom_group_kill")):
        return "validation cgroup memory limit was reached"
    pids = _event_counts(group.joinpath("pids.events"))
    if pids.get("max", 0) > 0:
        return "validation cgroup process limit was reached"
    return ""


def _remove_validation_cgroup(group: Path) -> None:
    kill_path = group.joinpath("cgroup.kill")
    if kill_path.exists():
        _write_cgroup_value(kill_path, "1")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        events = _event_counts(group.joinpath("cgroup.events"))
        if events.get("populated", 0) == 0:
            group.rmdir()
            return
        time.sleep(0.02)
    raise RuntimeError("validation cgroup remained populated after cleanup")


def _resident_bytes(pids: set[int]) -> int:
    page_size = os.sysconf("SC_PAGE_SIZE")
    total = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
        except FileNotFoundError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise RuntimeError(f"resident-memory evidence is unavailable for PID {pid}")
        if len(fields) < 2 or not fields[1].isdigit():
            raise RuntimeError(f"invalid resident-memory evidence for PID {pid}")
        total += int(fields[1]) * page_size
    return total


def _resource_limit_error(
    process_pid: int,
    *,
    scratch: Path,
    scratch_bytes: int,
    scratch_entries: int,
    baseline_bytes: int,
    baseline_entries: int,
    aggregate_memory_bytes: int,
    cgroup: Path | None,
) -> str:
    scratch_error = _scratch_limit_error(
        scratch,
        max_bytes=scratch_bytes,
        max_entries=scratch_entries,
        baseline_bytes=baseline_bytes,
        baseline_entries=baseline_entries,
    )
    if scratch_error:
        return scratch_error
    if cgroup is not None:
        return _cgroup_limit_error(cgroup)
    roots = {process_pid} | _direct_children(os.getpid(), required=True)
    resident_bytes = _resident_bytes(_descendant_tree(roots))
    if resident_bytes > aggregate_memory_bytes:
        return f"validation process tree exceeded {aggregate_memory_bytes} resident byte limit"
    return ""


def _signal_processes(pids: set[int], signal_number: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            pass


def _reap_children() -> None:
    while True:
        try:
            waited_pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == 0:
            return


def _terminate_descendants(root_pgid: int) -> None:
    try:
        os.killpg(root_pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 0.75
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            roots = _direct_children(os.getpid(), required=True)
            if not roots:
                _reap_children()
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= _DESCENDANT_QUIET_SEC:
                    return
                time.sleep(0.02)
                continue
            quiet_since = None
            _signal_processes(_descendant_tree(roots), signal_number)
            _reap_children()
            time.sleep(0.02)
    remaining = _direct_children(os.getpid(), required=True)
    if remaining:
        raise RuntimeError(f"descendant processes survived containment: {sorted(remaining)}")


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + abs(returncode)


def main(argv: Sequence[str]) -> int:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("process containment requires Linux")
    if not argv:
        raise ValueError("missing validation command")
    if os.environ.pop("NEXUS_TASK_COMMAND_MODE", "") == "1":
        return _run_task_command(argv)
    scratch = Path(os.environ.pop("NEXUS_VALIDATION_SCRATCH", ""))
    workspace = Path(os.environ.pop("NEXUS_VALIDATION_WORKSPACE", ""))
    cgroup_root_raw = os.environ.pop("NEXUS_VALIDATION_CGROUP_ROOT", "")
    allow_polling = os.environ.pop("NEXUS_TEST_VALIDATION_ALLOW_POLLING", "") == "1"
    file_bytes = _required_limit("NEXUS_VALIDATION_FILE_BYTES")
    open_files = _required_limit("NEXUS_VALIDATION_OPEN_FILES")
    processes = _required_limit("NEXUS_VALIDATION_PROCESSES")
    memory_bytes = _required_limit("NEXUS_VALIDATION_MEMORY_BYTES")
    aggregate_memory_bytes = _required_limit("NEXUS_VALIDATION_AGGREGATE_MEMORY_BYTES")
    scratch_bytes = _required_limit("NEXUS_VALIDATION_SCRATCH_BYTES")
    scratch_entries = _required_limit("NEXUS_VALIDATION_SCRATCH_ENTRIES")
    baseline_bytes = _required_nonnegative_limit("NEXUS_VALIDATION_BASELINE_BYTES")
    baseline_entries = _required_nonnegative_limit("NEXUS_VALIDATION_BASELINE_ENTRIES")
    if not scratch.is_absolute() or not scratch.is_dir():
        raise RuntimeError("validation scratch directory is unavailable")
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.parent != scratch:
        raise RuntimeError("staged validation workspace is unavailable")
    resolved_workspace = workspace.resolve()
    resolved_cwd = Path.cwd().resolve()
    if os.path.commonpath([str(resolved_workspace), str(resolved_cwd)]) != str(
        resolved_workspace
    ):
        raise RuntimeError("validation did not start in its staged workspace")
    uid, gid = _validation_identity(allow_current_user=allow_polling)
    _prepare_validation_tree(scratch, uid=uid, gid=gid)
    _set_child_subreaper()
    _direct_children(os.getpid(), required=True)
    signal.signal(signal.SIGTERM, _request_termination)
    signal.signal(signal.SIGINT, _request_termination)
    process: subprocess.Popen[bytes] | None = None
    cgroup: Path | None = None
    returncode = _CONTAINMENT_ERROR_EXIT
    cleanup_error: Exception | None = None
    resource_error = ""
    try:
        if cgroup_root_raw:
            cgroup = _create_validation_cgroup(
                Path(cgroup_root_raw),
                memory_bytes=aggregate_memory_bytes,
                processes=processes,
            )
        elif not allow_polling:
            raise RuntimeError("validation cgroup delegation is required")
        child_argv = (
            list(argv)
            if allow_polling
            else _landlocked_argv(argv, writable_root=scratch)
        )
        process = subprocess.Popen(
            child_argv,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=lambda: _apply_child_limits(
                uid=uid,
                gid=gid,
                file_bytes=file_bytes,
                open_files=open_files,
                processes=processes,
                memory_bytes=memory_bytes,
                cgroup=cgroup,
            ),
        )
        while process.poll() is None:
            resource_error = _resource_limit_error(
                process.pid,
                scratch=scratch,
                scratch_bytes=scratch_bytes,
                scratch_entries=scratch_entries,
                baseline_bytes=baseline_bytes,
                baseline_entries=baseline_entries,
                aggregate_memory_bytes=aggregate_memory_bytes,
                cgroup=cgroup,
            )
            if resource_error:
                break
            time.sleep(0.02)
        if not resource_error:
            resource_error = _resource_limit_error(
                process.pid,
                scratch=scratch,
                scratch_bytes=scratch_bytes,
                scratch_entries=scratch_entries,
                baseline_bytes=baseline_bytes,
                baseline_entries=baseline_entries,
                aggregate_memory_bytes=aggregate_memory_bytes,
                cgroup=cgroup,
            )
        returncode = _RESOURCE_ERROR_EXIT if resource_error else _exit_code(process.wait())
    except _TerminationRequested:
        returncode = 124
    finally:
        if process is not None:
            try:
                _terminate_descendants(process.pid)
            except Exception as exc:  # pragma: no cover - exceptional kernel failure
                cleanup_error = exc
        if cgroup is not None:
            try:
                _remove_validation_cgroup(cgroup)
            except Exception as exc:  # pragma: no cover - exceptional kernel failure
                cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise cleanup_error
    if resource_error:
        print(f"NEXUS_RESOURCE_ERROR: {resource_error}", file=sys.stderr)
    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"NEXUS_CONTAINMENT_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(_CONTAINMENT_ERROR_EXIT)
