from __future__ import annotations

import ctypes
import os
import pwd
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Sequence


_CONTAINMENT_ERROR_EXIT = 125
_DESCENDANT_QUIET_SEC = 0.25
_PR_SET_CHILD_SUBREAPER = 36
_RESOURCE_ERROR_EXIT = 125


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


def _validation_identity() -> tuple[int, int]:
    if os.geteuid() != 0:
        return os.geteuid(), os.getegid()
    account = pwd.getpwnam("nobody")
    return int(account.pw_uid), int(account.pw_gid)


def _apply_child_limits(
    *,
    uid: int,
    gid: int,
    file_bytes: int,
    open_files: int,
    processes: int,
    memory_bytes: int,
) -> None:
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


def _scratch_limit_error(root: Path, *, max_bytes: int, max_entries: int) -> str:
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
                if total_entries > max_entries:
                    return f"validation scratch exceeded {max_entries} entry limit"
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
                if total_bytes > max_bytes:
                    return f"validation scratch exceeded {max_bytes} byte limit"
    return ""


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
    aggregate_memory_bytes: int,
) -> str:
    scratch_error = _scratch_limit_error(
        scratch,
        max_bytes=scratch_bytes,
        max_entries=scratch_entries,
    )
    if scratch_error:
        return scratch_error
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
    scratch = Path(os.environ.pop("NEXUS_VALIDATION_SCRATCH", ""))
    file_bytes = _required_limit("NEXUS_VALIDATION_FILE_BYTES")
    open_files = _required_limit("NEXUS_VALIDATION_OPEN_FILES")
    processes = _required_limit("NEXUS_VALIDATION_PROCESSES")
    memory_bytes = _required_limit("NEXUS_VALIDATION_MEMORY_BYTES")
    aggregate_memory_bytes = _required_limit("NEXUS_VALIDATION_AGGREGATE_MEMORY_BYTES")
    scratch_bytes = _required_limit("NEXUS_VALIDATION_SCRATCH_BYTES")
    scratch_entries = _required_limit("NEXUS_VALIDATION_SCRATCH_ENTRIES")
    if not scratch.is_absolute() or not scratch.is_dir():
        raise RuntimeError("validation scratch directory is unavailable")
    uid, gid = _validation_identity()
    if os.geteuid() == 0:
        os.chown(scratch, uid, gid)
    scratch.chmod(0o700)
    _set_child_subreaper()
    _direct_children(os.getpid(), required=True)
    signal.signal(signal.SIGTERM, _request_termination)
    signal.signal(signal.SIGINT, _request_termination)
    process: subprocess.Popen[bytes] | None = None
    returncode = _CONTAINMENT_ERROR_EXIT
    cleanup_error: Exception | None = None
    resource_error = ""
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=lambda: _apply_child_limits(
                uid=uid,
                gid=gid,
                file_bytes=file_bytes,
                open_files=open_files,
                processes=processes,
                memory_bytes=memory_bytes,
            ),
        )
        while process.poll() is None:
            resource_error = _resource_limit_error(
                process.pid,
                scratch=scratch,
                scratch_bytes=scratch_bytes,
                scratch_entries=scratch_entries,
                aggregate_memory_bytes=aggregate_memory_bytes,
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
                aggregate_memory_bytes=aggregate_memory_bytes,
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
