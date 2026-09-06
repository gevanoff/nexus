from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Sequence


_CONTAINMENT_ERROR_EXIT = 125
_PR_SET_CHILD_SUBREAPER = 36


class _TerminationRequested(Exception):
    pass


def _request_termination(_signal_number: int, _frame: object) -> NoReturn:
    raise _TerminationRequested()


def _set_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _direct_children(pid: int) -> set[int]:
    children: set[int] = set()
    try:
        tasks = os.scandir(f"/proc/{pid}/task")
    except FileNotFoundError:
        return children
    with tasks:
        for task in tasks:
            if not task.name.isdigit():
                continue
            try:
                text = Path(task.path, "children").read_text(encoding="ascii")
            except (FileNotFoundError, ProcessLookupError):
                continue
            children.update(int(value) for value in text.split() if value.isdigit())
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
        while time.monotonic() < deadline:
            roots = _direct_children(os.getpid())
            if not roots:
                return
            _signal_processes(_descendant_tree(roots), signal_number)
            _reap_children()
            time.sleep(0.02)
    remaining = _direct_children(os.getpid())
    if remaining:
        raise RuntimeError(f"descendant processes survived containment: {sorted(remaining)}")


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + abs(returncode)


def main(argv: Sequence[str]) -> int:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("process containment requires Linux")
    if not argv:
        raise ValueError("missing validation command")
    _set_child_subreaper()
    signal.signal(signal.SIGTERM, _request_termination)
    signal.signal(signal.SIGINT, _request_termination)
    process: subprocess.Popen[bytes] | None = None
    returncode = _CONTAINMENT_ERROR_EXIT
    cleanup_error: Exception | None = None
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
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


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"NEXUS_CONTAINMENT_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(_CONTAINMENT_ERROR_EXIT)
