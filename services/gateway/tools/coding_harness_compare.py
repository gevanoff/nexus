#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit, urlunsplit

ALBATROSS_TESTED_VERSION = "2.4.0"
ALBATROSS_TESTED_COMMIT = "6f20178d81c6f0fdbb97ccf826b0d56f04a77faf"
FIXTURE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
CHARACTER_ESCAPE_RE = re.compile(
    rb"\\[uU]\{([0-9a-fA-F]{1,6})\}"
    rb"|\\[uU]([0-9a-fA-F]{8})"
    rb"|\\[uU]([0-9a-fA-F]{4})"
    rb"|\\[xX]([0-9a-fA-F]{2})"
    rb"|\\([0-7]{3})"
    rb"|%([0-9a-fA-F]{2})"
    rb"|&#([0-9]{1,7});"
    rb"|&#[xX]([0-9a-fA-F]{1,6});"
)
RESERVED_PARTS = {".git", ".nexus", ".albatross", ".small-harness", ".sessions"}
RESERVED_FILES = {"agent.config.json", ".env"}
ALBATROSS_TOOLS = "apply_patch,file_read,file_write,file_edit,glob,grep,list_dir,update_plan"
ALBATROSS_READ_ONLY_TOOLS = "file_read,glob,grep,list_dir"
REQUIRED_PROBE_CAPABILITIES = ("one_shot", "allow_tools")
MAX_FIXTURE_FILE_BYTES = 2_000_000
MAX_FIXTURE_TOTAL_BYTES = 8_000_000
MAX_FIXTURE_JSON_BYTES = 10_000_000
MAX_FIXTURE_FILES = 4096
MAX_MISSION_BYTES = 64_000
MAX_OBJECTIVE_FILE_BYTES = 2_000_000
MAX_OBJECTIVE_CHECKS = 256
MAX_PROCESS_OUTPUT_CHARS = 100_000
MAX_GIT_EVIDENCE_CHARS = 8_000_000
MAX_TRACE_FILE_BYTES = 8_000_000
MAX_TRACE_TOTAL_BYTES = 16_000_000
MIN_CROSS_FIELD_FRAGMENT_BYTES = 2
MIN_UNORDERED_FRAGMENT_BYTES = 8
MAX_FRAGMENT_SEARCH_STATES = 4096
RESULT_CHILD_CONTROLLED_FIELDS = frozenset(
    {
        "actual",
        "argv",
        "error",
        "files_changed",
        "omitted_non_text",
        "path",
        "reason",
        "stderr",
        "stdout",
        "tool_call_names",
    }
)
MAX_TRACE_STEP_DIGITS = 18
MAX_TRACE_TURN_DIGITS = 18
MAX_TRACE_FILES = 256
MAX_TRACE_ENTRIES = 4096
MAX_TRACE_PARSE_SECONDS = 10.0
MAX_TRACE_AGENT_STEPS = 100
MAX_SNAPSHOT_CHANGED_FILES = 512
MAX_SNAPSHOT_FILE_BYTES = 16_000_000
MAX_SNAPSHOT_DIFF_CHARS = 8_000_000
MAX_SNAPSHOT_SECONDS = 30.0
MAX_VALIDATION_SCRATCH_BYTES = 64 * 1024 * 1024
MAX_VALIDATION_SCRATCH_ENTRIES = 4096
MAX_VALIDATION_FILE_BYTES = 1024 * 1024
MAX_VALIDATION_OPEN_FILES = 64
MAX_VALIDATION_PROCESSES = 128
MAX_VALIDATION_MEMORY_BYTES = 16 * 1024 * 1024 * 1024
MAX_VALIDATION_AGGREGATE_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
MAX_NEXUS_API_RESPONSE_BYTES = 8_000_000
NEXUS_POLL_INTERVAL_SEC = 2.0
NEXUS_MIN_AGENT_STEPS = 4
NEXUS_MIN_WALL_TIME_SEC = 60
NEXUS_GUARDED_WORKER_TIMEOUT_SEC = 120.0
NEXUS_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "failed_finalization",
        "failed_publish",
        "paused",
        "stopped",
        "interrupted",
        "idle_waiting",
    }
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_rel_path(value: str) -> Path:
    raw = "" if value is None else str(value)
    raw_parts = raw.split("/")
    if (
        raw == ""
        or len(raw.encode("utf-8", errors="surrogateescape")) > 4096
        or any(part in {"", ".", ".."} for part in raw_parts)
        or "\\" in raw
        or "\x00" in raw
        or any(ord(char) < 32 for char in raw)
    ):
        raise ValueError(f"unsafe fixture path: {value}")
    path = Path(*raw_parts)
    if path.is_absolute():
        raise ValueError(f"unsafe fixture path: {value}")
    if (
        any(part.casefold() in RESERVED_PARTS for part in path.parts)
        or path.name.casefold() in RESERVED_FILES
    ):
        raise ValueError(f"fixture path may not override harness state/config: {value}")
    return path


def fixture_rel_path(value: str) -> Path:
    raw = "" if value is None else str(value)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"unsafe fixture path: {value}") from exc
    return safe_rel_path(raw)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    secrets = tuple(str(secret) for secret in secrets if secret)
    out = str(text or "")
    for secret in secrets:
        out = out.replace(secret, "(redacted)")
    out = re.sub(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1(redacted)", out)
    out = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "(redacted)", out)
    out = re.sub(r"(?i)\bsk-(?:or-)?[A-Za-z0-9_-]{8,}\b", "(redacted)", out)
    if _contains_encoded_secret_bytes(out.encode("utf-8", errors="replace"), secrets):
        return "(redacted)"
    if _fragmented_secret_indexes(
        [out.encode("utf-8", errors="replace")], secrets
    ):
        return "(redacted)"
    return out


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_value(v, secrets) for v in value]
    if isinstance(value, dict):
        return {str(k): "(redacted)" if SECRET_KEY_RE.search(str(k)) else redact_value(v, secrets)
                for k, v in value.items()}
    return value


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        fixture_size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat fixture: {path}: {exc}") from exc
    if fixture_size > MAX_FIXTURE_JSON_BYTES:
        raise ValueError(f"fixture JSON exceeds {MAX_FIXTURE_JSON_BYTES} byte limit")
    fixture = read_json(path)
    if not isinstance(fixture, dict):
        raise ValueError("fixture must be a JSON object")
    if int(fixture.get("schema_version") or 0) != FIXTURE_SCHEMA_VERSION:
        raise ValueError(f"fixture schema_version must be {FIXTURE_SCHEMA_VERSION}")
    fid = str(fixture.get("id") or "")
    if not SAFE_ID_RE.fullmatch(fid):
        raise ValueError("fixture id contains unsafe characters")
    mission = fixture.get("mission")
    if not isinstance(mission, str) or not mission.strip():
        raise ValueError(f"fixture {fid} has no mission")
    try:
        mission_bytes = len(mission.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"fixture {fid} mission must be valid UTF-8 text") from exc
    if mission_bytes > MAX_MISSION_BYTES:
        raise ValueError(f"fixture {fid} mission exceeds {MAX_MISSION_BYTES} byte limit")
    repo = fixture.get("repository")
    files = repo.get("files") if isinstance(repo, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError(f"fixture {fid} repository.files must be non-empty")
    if len(files) > MAX_FIXTURE_FILES:
        raise ValueError(
            f"fixture {fid} repository.files exceeds {MAX_FIXTURE_FILES} entry limit"
        )
    normalized_files: dict[str, str] = {}
    total_file_bytes = 0
    for raw_path, raw_content in files.items():
        rel = fixture_rel_path(str(raw_path)).as_posix()
        if not isinstance(raw_content, str):
            raise ValueError(f"fixture file {rel} content must be a string")
        content = raw_content
        size = len(content.encode("utf-8"))
        if size > MAX_FIXTURE_FILE_BYTES:
            raise ValueError(f"fixture file {rel} exceeds {MAX_FIXTURE_FILE_BYTES} byte limit")
        total_file_bytes += size
        if total_file_bytes > MAX_FIXTURE_TOTAL_BYTES:
            raise ValueError(f"fixture inline files exceed {MAX_FIXTURE_TOTAL_BYTES} byte total limit")
        normalized_files[rel] = content
    repo["files"] = normalized_files
    expected = fixture.setdefault("expected", {})
    if not isinstance(expected, dict):
        raise ValueError("expected must be an object")
    for key in ("files_changed", "allowed_files_changed"):
        if key in expected:
            if not isinstance(expected[key], list):
                raise ValueError(f"expected.{key} must be an array")
            expected[key] = [fixture_rel_path(str(v)).as_posix() for v in expected[key]]
    content_check_count = 0
    for key in ("file_contains", "file_not_contains"):
        checks = expected.get(key) or []
        if not isinstance(checks, list):
            raise ValueError(f"expected.{key} must be an array")
        content_check_count += len(checks)
        if content_check_count > MAX_OBJECTIVE_CHECKS:
            raise ValueError(
                f"content objectives exceed {MAX_OBJECTIVE_CHECKS} check limit"
            )
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError(f"expected.{key} entries must be objects")
            check["path"] = fixture_rel_path(str(check.get("path") or "")).as_posix()
            if not str(check.get("needle") or ""):
                raise ValueError(f"expected.{key} needle must be non-empty")
    validation = expected.get("validation") or []
    if "validation" in expected and not isinstance(expected.get("validation"), list):
        raise ValueError("expected.validation must be an array")
    for command in validation:
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError("validation commands must be non-empty argv arrays")
    has_verification = (
        "files_changed" in expected
        or "allowed_files_changed" in expected
        or bool(expected.get("file_contains"))
        or bool(expected.get("file_not_contains"))
        or bool(validation)
    )
    if not has_verification:
        raise ValueError("expected must define at least one objective check or validation command")
    limits = fixture.setdefault("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    limits["wall_time_sec"] = int(limits.get("wall_time_sec") or 300)
    limits["max_agent_steps"] = int(limits.get("max_agent_steps") or 20)
    if not 5 <= limits["wall_time_sec"] <= 3600:
        raise ValueError("wall_time_sec must be between 5 and 3600")
    if not 1 <= limits["max_agent_steps"] <= MAX_TRACE_AGENT_STEPS:
        raise ValueError(
            f"max_agent_steps must be between 1 and {MAX_TRACE_AGENT_STEPS}"
        )
    return fixture


def clean_env(*, home: Path | None = None, temp_dir: Path | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if os.environ.get(k)}
    if home is not None:
        env["HOME"] = str(home)
    if temp_dir is not None:
        env["TMPDIR"] = str(temp_dir)
    env["NO_COLOR"] = "1"
    return env


def _validation_sandbox_argv(
    argv: list[str], workspace: Path, home: Path, temp_dir: Path
) -> tuple[list[str], dict[str, str]]:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("validation sandbox requires a Linux host")
    if os.getuid() == 0 or os.geteuid() == 0:
        raise RuntimeError(
            "validation sandbox requires a non-root host user for an enforceable task limit"
        )
    bubblewrap = shutil.which("bwrap", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if bubblewrap is None:
        raise RuntimeError("validation sandbox requires bubblewrap (bwrap)")
    prlimit = shutil.which("prlimit", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if prlimit is None:
        raise RuntimeError("validation sandbox requires prlimit")
    unshare = shutil.which("unshare", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if unshare is None:
        raise RuntimeError("validation sandbox requires unshare")
    mount = shutil.which("mount", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if mount is None:
        raise RuntimeError("validation sandbox requires mount")
    shell = shutil.which("sh", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if shell is None:
        raise RuntimeError("validation sandbox requires sh")

    trusted: dict[str, Path] = {}
    for label, path in (
        ("workspace", workspace),
        ("agent home", home),
        ("temporary directory", temp_dir),
    ):
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"could not verify validation {label}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"validation {label} is not a trusted directory")
        trusted[label] = resolved
    trusted_paths = list(trusted.values())
    if any(
        left == right or left in right.parents or right in left.parents
        for index, left in enumerate(trusted_paths)
        for right in trusted_paths[index + 1:]
    ):
        raise RuntimeError("validation workspace, agent home, and temporary directory must be disjoint")

    validation_home = Path("/tmp") / f".validation-home-{uuid.uuid4().hex}"
    validation_temp = Path("/tmp")
    child_env = clean_env(home=validation_home, temp_dir=validation_temp)
    child_env = {
        key: value for key, value in child_env.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "HOME", "TMPDIR", "NO_COLOR"}
    }
    child_env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    command = [
        bubblewrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop", "ALL",
        "--clearenv",
        "--ro-bind", "/usr", "/usr",
    ]
    for system_path in (Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
        if system_path.is_symlink():
            command.extend(("--symlink", os.readlink(system_path), str(system_path)))
        elif system_path.is_dir():
            command.extend(("--ro-bind", str(system_path), str(system_path)))
    for system_path in (
        Path("/etc/ld.so.cache"),
        Path("/etc/ld.so.conf"),
        Path("/etc/ld.so.conf.d"),
        Path("/etc/localtime"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/ssl/certs"),
        Path("/etc/ca-certificates.conf"),
    ):
        if system_path.exists():
            command.extend(("--ro-bind", str(system_path), str(system_path)))
    command.extend((
        "--dev", "/dev",
        "--proc", "/proc",
        "--dir", "/tmp",
        "--remount-ro", "/",
        "--bind", str(trusted["temporary directory"]), "/tmp",
        "--dir", str(validation_home),
        "--chmod", "0700", str(validation_home),
        "--ro-bind", str(trusted["workspace"]), str(trusted["workspace"]),
    ))
    for key, value in sorted(child_env.items()):
        command.extend(("--setenv", key, value))
    command.extend(("--chdir", str(trusted["workspace"]), "--", *argv))
    mount_script = (
        'mount_path=$1; scratch=$2; options=$3; shift 3; '
        '"$mount_path" -t tmpfs -o "$options" tmpfs "$scratch" || exit $?; '
        'exec "$@"'
    )
    tmpfs_options = (
        f"size={MAX_VALIDATION_SCRATCH_BYTES},"
        f"nr_inodes={MAX_VALIDATION_SCRATCH_ENTRIES},mode=0700"
    )
    wrapped = [
        prlimit,
        f"--fsize={MAX_VALIDATION_FILE_BYTES}:{MAX_VALIDATION_FILE_BYTES}",
        f"--nofile={MAX_VALIDATION_OPEN_FILES}:{MAX_VALIDATION_OPEN_FILES}",
        f"--nproc={MAX_VALIDATION_PROCESSES}:{MAX_VALIDATION_PROCESSES}",
        f"--as={MAX_VALIDATION_MEMORY_BYTES}:{MAX_VALIDATION_MEMORY_BYTES}",
        "--",
        unshare,
        "--user",
        "--map-root-user",
        "--mount",
        "--",
        shell,
        "-c",
        mount_script,
        "validation-scratch",
        mount,
        str(trusted["temporary directory"]),
        tmpfs_options,
        *command,
    ]
    return wrapped, clean_env()


def _process_group_alive(pgid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_group(pgid: int, grace_sec: float = 0.5) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, grace_sec)
    while time.monotonic() < deadline:
        if not _process_group_alive(pgid):
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and _process_group_alive(pgid):
        time.sleep(0.02)


def _linux_direct_children(pid: int) -> set[int]:
    children: set[int] = set()
    with os.scandir(f"/proc/{pid}/task") as tasks:
        for task in tasks:
            if not task.name.isdigit():
                continue
            try:
                if not task.is_dir(follow_symlinks=False):
                    continue
                text = Path(task.path, "children").read_text(encoding="ascii")
            except FileNotFoundError:
                try:
                    os.stat(task.path, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise
            children.update(int(value) for value in text.split() if value.isdigit())
    return children


def _linux_subreaper_enabled() -> bool:
    value = ctypes.c_int()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(37, ctypes.byref(value), 0, 0, 0) != 0:  # PR_GET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return bool(value.value)


def _set_linux_subreaper(enabled: bool) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, int(enabled), 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _linux_descendant_tree(roots: Iterable[int]) -> set[int]:
    found: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            children = _linux_direct_children(pid)
        except FileNotFoundError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise
        pending.extend(children - found)
    return found


def _terminate_linux_adopted_children(baseline_children: set[int]) -> None:
    parent_pid = os.getpid()
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            roots = _linux_direct_children(parent_pid) - baseline_children
            if not roots:
                return
            for pid in _linux_descendant_tree(roots):
                try:
                    os.kill(pid, signal_number)
                except ProcessLookupError:
                    pass
            for pid in roots:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass
            time.sleep(0.02)
    remaining = _linux_direct_children(parent_pid) - baseline_children
    if remaining:
        raise RuntimeError(f"descendant processes survived containment: {sorted(remaining)}")


def _drain_stream(stream: Any, limit_chars: int | None, state: dict[str, Any],
                  redact_overlap_chars: int = 0) -> None:
    chunks: deque[str] = deque()
    retained = 0
    total = 0
    retention_limit = None if limit_chars is None else limit_chars + max(0, redact_overlap_chars)
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                break
            total += len(chunk)
            if retention_limit is None:
                chunks.append(chunk)
                retained += len(chunk)
                continue
            if retention_limit <= 0:
                continue
            chunks.append(chunk)
            retained += len(chunk)
            while retained > retention_limit and chunks:
                overflow = retained - retention_limit
                first = chunks[0]
                if len(first) <= overflow:
                    retained -= len(chunks.popleft())
                else:
                    chunks[0] = first[overflow:]
                    retained -= overflow
    except OSError as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            stream.close()
        except OSError:
            pass
    state["text"] = "".join(chunks)
    state["truncated"] = bool(limit_chars is not None and total > limit_chars)


def _scratch_limit_error(
    root: Path, *, max_bytes: int, max_entries: int
) -> str | None:
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
            return f"could not inspect validation scratch space: {exc}"
        try:
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
                            if total_bytes > max_bytes:
                                return f"validation scratch exceeded {max_bytes} byte limit"
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        return f"could not inspect validation scratch entry: {exc}"
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"could not scan validation scratch space: {exc}"
    return None


def _linux_pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _linux_validation_tree_usage(
    root_pid: int, baseline_children: set[int]
) -> int:
    roots = {root_pid} | (_linux_direct_children(os.getpid()) - baseline_children)
    pids = _linux_descendant_tree(roots)
    page_size = os.sysconf("SC_PAGE_SIZE")
    resident_bytes = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
        except FileNotFoundError:
            if _linux_pid_is_gone(pid):
                continue
            raise
        if len(fields) < 2 or not fields[1].isdigit():
            raise RuntimeError(f"invalid resident-memory evidence for PID {pid}")
        resident_bytes += int(fields[1]) * page_size
    return resident_bytes


def _validation_tree_limit_error(
    root_pid: int,
    baseline_children: set[int],
    *,
    max_resident_bytes: int,
) -> tuple[str | None, str | None]:
    try:
        resident_bytes = _linux_validation_tree_usage(root_pid, baseline_children)
    except (OSError, RuntimeError) as exc:
        return (
            "validation_resource_inspection_failed",
            f"could not inspect validation process-tree resources: {exc}",
        )
    if resident_bytes > max_resident_bytes:
        return (
            "validation_memory_limit_exceeded",
            f"validation process tree exceeded {max_resident_bytes} resident byte limit",
        )
    return None, None


def run_process(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                timeout_sec: float = 60.0, secrets: Iterable[str] = (),
                isolate_process_group: bool = False,
                require_descendant_containment: bool = True,
                output_limit_chars: int | None = MAX_PROCESS_OUTPUT_CHARS,
                fail_on_output_limit: bool = False,
                decode_errors: str = "replace",
                include_raw_output: bool = False,
                scratch_dir: Path | None = None,
                scratch_max_bytes: int | None = None,
                scratch_max_entries: int | None = None,
                aggregate_memory_max_bytes: int | None = None) -> dict[str, Any]:
    started = time.monotonic()
    secrets = tuple(str(secret) for secret in secrets if secret)
    stdout_state: dict[str, Any] = {}
    stderr_state: dict[str, Any] = {}
    stream_threads: list[tuple[Any, Any, dict[str, Any]]] = []
    timed_out = False
    scratch_error: str | None = None
    resource_error: str | None = None
    resource_error_kind: str | None = None
    containment_error: str | None = None
    redact_overlap_chars = max(
        (
            length
            for secret in secrets
            for length in (
                len(secret),
                *(len(encoded) for encoded in _encoded_secret_variants(secret)),
            )
        ),
        default=0,
    )
    if isolate_process_group and not sys.platform.startswith("linux"):
        return {"ok": False, "returncode": None, "timed_out": False,
                "stdout": "", "stderr": "descendant process containment requires a Linux host",
                "duration_ms": 0.0, "launch_error": "process_group_unsupported",
                "output_truncated": False}
    baseline_children: set[int] = set()
    subreaper_was_enabled = False
    adopted_containment_enabled = False
    if isolate_process_group:
        try:
            subreaper_was_enabled = _linux_subreaper_enabled()
            baseline_children = _linux_direct_children(os.getpid())
            if not subreaper_was_enabled:
                _set_linux_subreaper(True)
            adopted_containment_enabled = True
        except OSError as exc:
            if not subreaper_was_enabled:
                with contextlib.suppress(OSError):
                    _set_linux_subreaper(False)
            if require_descendant_containment:
                return {"ok": False, "returncode": None, "timed_out": False,
                        "stdout": "", "stderr": f"could not enable descendant containment: {exc}",
                        "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
                        "launch_error": "subreaper_unavailable", "output_truncated": False}
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors=decode_errors,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=bool(isolate_process_group),
        )
    except OSError as exc:
        restore_error: OSError | None = None
        if adopted_containment_enabled and not subreaper_was_enabled:
            try:
                _set_linux_subreaper(False)
            except OSError as restore_exc:
                restore_error = restore_exc
        stderr = redact_text(f"{type(exc).__name__}: {exc}", secrets)
        if restore_error is not None:
            stderr += f"\ncould not restore subreaper state: {restore_error}"
        return {"ok": False, "returncode": None, "timed_out": False,
                "stdout": "", "stderr": stderr,
                "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
                "launch_error": type(exc).__name__, "output_truncated": False}
    except BaseException as launch_exc:
        containment_error: str | None = None
        if adopted_containment_enabled:
            try:
                _terminate_linux_adopted_children(baseline_children)
            except (OSError, RuntimeError) as exc:
                containment_error = f"could not contain interrupted process launch: {exc}"
            finally:
                if not subreaper_was_enabled:
                    try:
                        _set_linux_subreaper(False)
                    except OSError as exc:
                        containment_error = containment_error or f"could not restore subreaper state: {exc}"
        if containment_error:
            raise RuntimeError(f"SECURITY: {containment_error}") from launch_exc
        raise

    try:
        stream_threads = [
            (threading.Thread(
                target=_drain_stream,
                args=(proc.stdout, output_limit_chars, stdout_state, redact_overlap_chars),
                daemon=True,
            ), proc.stdout, stdout_state),
            (threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, output_limit_chars, stderr_state, redact_overlap_chars),
                daemon=True,
            ), proc.stderr, stderr_state),
        ]
        for thread, _, _ in stream_threads:
            thread.start()
        wait_deadline = time.monotonic() + max(0.001, timeout_sec)
        while proc.poll() is None:
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                proc.wait(timeout=min(0.02, remaining))
            except subprocess.TimeoutExpired:
                if (
                    scratch_dir is not None
                    and scratch_max_bytes is not None
                    and scratch_max_entries is not None
                ):
                    scratch_error = _scratch_limit_error(
                        scratch_dir,
                        max_bytes=scratch_max_bytes,
                        max_entries=scratch_max_entries,
                    )
                    if scratch_error:
                        break
                if aggregate_memory_max_bytes is not None:
                    resource_error_kind, resource_error = _validation_tree_limit_error(
                        proc.pid,
                        baseline_children,
                        max_resident_bytes=aggregate_memory_max_bytes,
                    )
                    if resource_error:
                        break
        if (
            scratch_error is None
            and scratch_dir is not None
            and scratch_max_bytes is not None
            and scratch_max_entries is not None
        ):
            scratch_error = _scratch_limit_error(
                scratch_dir,
                max_bytes=scratch_max_bytes,
                max_entries=scratch_max_entries,
            )
    finally:
        try:
            if isolate_process_group:
                _terminate_process_group(proc.pid)
            elif proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if adopted_containment_enabled:
                try:
                    _terminate_linux_adopted_children(baseline_children)
                except (OSError, RuntimeError) as exc:
                    containment_error = f"could not verify descendant containment: {exc}"
                finally:
                    if not subreaper_was_enabled:
                        try:
                            _set_linux_subreaper(False)
                        except OSError as exc:
                            containment_error = containment_error or f"could not restore subreaper state: {exc}"
            for thread, stream, state in stream_threads:
                if thread.ident is None:
                    continue
                thread.join(timeout=2.0)
                if thread.is_alive():
                    try:
                        stream.close()
                    except OSError:
                        pass
                    thread.join(timeout=0.5)
                    if thread.is_alive():
                        state["error"] = "stream drain did not terminate"
            if containment_error:
                raise RuntimeError(f"SECURITY: {containment_error}")

    raw_stdout = str(stdout_state.get("text") or "")
    raw_stderr = str(stderr_state.get("text") or "")
    stdout_truncated = bool(stdout_state.get("truncated"))
    stderr_truncated = bool(stderr_state.get("truncated"))
    stream_error = stdout_state.get("error") or stderr_state.get("error")
    output_truncated = stdout_truncated or stderr_truncated or bool(stream_error)
    clean_stdout = redact_text(raw_stdout, secrets)
    clean_stderr = redact_text(raw_stderr, secrets)
    if (
        scratch_error is None
        and scratch_dir is not None
        and proc.returncode not in (None, 0)
        and "No space left on device" in clean_stderr
    ):
        scratch_error = "validation scratch exceeded its filesystem quota"
    bounded_raw_stdout = raw_stdout
    bounded_raw_stderr = raw_stderr
    if output_limit_chars is not None:
        if stdout_truncated:
            clean_stdout = clean_stdout[-output_limit_chars:] if output_limit_chars > 0 else ""
            bounded_raw_stdout = raw_stdout[-output_limit_chars:] if output_limit_chars > 0 else ""
        if stderr_truncated:
            clean_stderr = clean_stderr[-output_limit_chars:] if output_limit_chars > 0 else ""
            bounded_raw_stderr = raw_stderr[-output_limit_chars:] if output_limit_chars > 0 else ""
    if secrets:
        if stdout_truncated:
            clean_stdout = "(redacted)" if raw_stdout else ""
            bounded_raw_stdout = ""
        if stderr_truncated:
            clean_stderr = "(redacted)" if raw_stderr else ""
            bounded_raw_stderr = ""
    if timed_out:
        clean_stderr = f"timeout after {timeout_sec}s\n{clean_stderr}"
    if stream_error:
        clean_stderr = f"stream read error: {stream_error}\n{clean_stderr}"
    if scratch_error:
        clean_stderr = f"{scratch_error}\n{clean_stderr}"
    if resource_error:
        clean_stderr = f"{resource_error}\n{clean_stderr}"
    if output_truncated and fail_on_output_limit:
        clean_stderr = f"output exceeded {output_limit_chars} character evidence limit\n{clean_stderr}"
    ok = bool(not timed_out and not stream_error and not scratch_error and not resource_error
              and proc.returncode == 0
              and not (output_truncated and fail_on_output_limit))
    result = {"ok": ok, "returncode": None if timed_out else proc.returncode,
              "timed_out": timed_out,
              "stdout": clean_stdout,
              "stderr": clean_stderr,
              "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
              "launch_error": (
                  resource_error_kind
                  or ("scratch_limit_exceeded" if scratch_error else None)
              ),
              "output_truncated": output_truncated}
    if include_raw_output:
        result["_raw_stdout"] = bounded_raw_stdout
        result["_raw_stderr"] = bounded_raw_stderr
    return result


def _git_env() -> dict[str, str]:
    env = clean_env()
    env.update({
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def git(
    argv: list[str], *, cwd: Path, evidence: bool = False, path_output: bool = False,
    secrets: Iterable[str] = (), include_raw_output: bool = False,
    extra_env: dict[str, str] | None = None, timeout_sec: float = 60.0,
) -> dict[str, Any]:
    env = _git_env()
    if extra_env:
        env.update(extra_env)
    return run_process(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "core.fileMode=true", *argv],
        cwd=cwd,
        env=env,
        timeout_sec=timeout_sec,
        output_limit_chars=MAX_GIT_EVIDENCE_CHARS if evidence else MAX_PROCESS_OUTPUT_CHARS,
        fail_on_output_limit=evidence,
        decode_errors="surrogateescape" if path_output else "backslashreplace",
        secrets=secrets,
        include_raw_output=include_raw_output,
    )


def _require_result(result: dict[str, Any], label: str, *, allowed_returncodes: tuple[int, ...] = (0,),
                    secrets: Iterable[str] = ()) -> dict[str, Any]:
    if (result.get("timed_out") or result.get("launch_error") or result.get("output_truncated")
            or result.get("returncode") not in allowed_returncodes):
        detail = result.get("stderr") or result.get("stdout") or f"returncode={result.get('returncode')}"
        raise RuntimeError(redact_text(f"{label} failed: {detail}", secrets))
    return result


def _required_git(
    argv: list[str], *, cwd: Path, label: str | None = None, path_output: bool = False,
    secrets: Iterable[str] = (), extra_env: dict[str, str] | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    secrets = tuple(str(secret) for secret in secrets if secret)
    result = git(
        argv,
        cwd=cwd,
        evidence=True,
        path_output=path_output,
        secrets=secrets,
        include_raw_output=True,
        extra_env=extra_env,
        timeout_sec=timeout_sec,
    )
    checked = _require_result(result, label or f"git {' '.join(argv)}", secrets=secrets)
    checked["stdout"] = checked.pop("_raw_stdout", checked["stdout"])
    checked["stderr"] = checked.pop("_raw_stderr", checked["stderr"])
    return checked


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(path, 0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"private run directory could not be secured: {path}")


def _ensure_private_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _path_lexists(path):
        _mkdir_private(path)
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"private directory path is not a trusted directory: {path}")
    if os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
                raise RuntimeError(f"private directory is not owned by the current user: {path}")
            os.fchmod(fd, 0o700)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise RuntimeError(f"private directory mode could not be secured: {path}")
        finally:
            os.close(fd)
    else:
        os.chmod(path, 0o700)
        secured = path.lstat()
        if (stat.S_ISLNK(secured.st_mode) or not stat.S_ISDIR(secured.st_mode)
                or stat.S_IMODE(secured.st_mode) != 0o700):
            raise RuntimeError(f"private directory mode could not be secured: {path}")


def initialize_workspace(workspace: Path, fixture: dict[str, Any]) -> str:
    _mkdir_private(workspace)
    root = workspace.resolve()
    for rel, content in fixture["repository"]["files"].items():
        target = (workspace / rel).resolve()
        if root not in target.parents:
            raise ValueError(f"fixture path escapes workspace: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    result = git(["init"], cwd=workspace)
    if not result["ok"]:
        raise RuntimeError(f"git init failed: {result['stderr'] or result['stdout']}")
    _sanitize_snapshot_git_metadata(workspace)
    for argv in (["config", "user.email", "coding-harness@example.invalid"],
                 ["config", "user.name", "Coding Harness Eval"], ["add", "--force", "."],
                 ["commit", "-m", "fixture baseline", "--allow-empty"]):
        result = git(list(argv), cwd=workspace)
        if not result["ok"]:
            raise RuntimeError(f"git {' '.join(argv)} failed: {result['stderr'] or result['stdout']}")
    with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n# Nexus harness runtime\n"
            ".albatross/\n"
            ".small-harness/\n"
            ".sessions/\n"
            "/agent.config.json\n"
        )
    head = git(["rev-parse", "HEAD"], cwd=workspace)
    if not head["ok"]:
        raise RuntimeError("could not determine fixture baseline")
    return head["stdout"].strip()


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _repair_tree_permissions(path: Path) -> None:
    if not _path_lexists(path):
        return
    try:
        root_info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass
    if not path.is_dir():
        return
    for current, dirs, _files in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            os.chmod(current_path, stat.S_IRWXU)
        except OSError:
            pass
        for name in dirs:
            child = current_path / name
            if not child.is_symlink():
                try:
                    os.chmod(child, stat.S_IRWXU)
                except OSError:
                    pass


def discard_path_verified(path: Path) -> None:
    if not _path_lexists(path):
        return
    last_error: Exception | None = None
    for _ in range(2):
        _repair_tree_permissions(path)
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as exc:
            last_error = exc
        if not _path_lexists(path):
            return
    raise RuntimeError(f"could not securely discard {path}: {last_error or 'path still exists'}")


def discard_run_root_verified(root: Path) -> None:
    discard_path_verified(root)
    if _path_lexists(root):
        raise RuntimeError(f"SECURITY: run root still exists after discard attempt: {root}")


def _prepare_artifacts_root(root: Path, artifacts: Path) -> None:
    if artifacts.parent != root:
        raise RuntimeError("artifact root is outside the run root")
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError("execution root is not a trusted directory")
    if _path_lexists(artifacts):
        discard_path_verified(artifacts)
    artifacts.mkdir(mode=0o700, parents=False, exist_ok=False)
    info = artifacts.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is not a trusted directory")


def _require_expected_directory_entries(
    path: Path, expected: set[str], *, allowed: set[str] | None = None
) -> None:
    try:
        info = path.lstat()
        entries = {entry.name for entry in path.iterdir()}
    except OSError as exc:
        raise RuntimeError(f"could not verify retained run directory structure: {exc}") from exc
    permitted = expected if allowed is None else allowed
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or not expected <= entries or not entries <= permitted):
        raise RuntimeError("unexpected entries remained in the retained run directory structure")


def _parse_nul_paths(text: str) -> list[str]:
    return sorted(set(value for value in str(text or "").split("\0") if value))


def _workspace_regular_file(workspace: Path, rel: str, *, max_bytes: int | None = None) -> tuple[Path | None, str | None]:
    try:
        safe = safe_rel_path(rel)
    except ValueError as exc:
        return None, str(exc)
    current = workspace
    try:
        root_stat = workspace.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            return None, "workspace root is not a regular directory"
        for index, part in enumerate(safe.parts):
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None, "path contains a symlink"
            if index < len(safe.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return None, "path parent is not a directory"
        if not stat.S_ISREG(info.st_mode):
            return None, "path is not a regular file"
        if max_bytes is not None and info.st_size > max_bytes:
            return None, f"file exceeds {max_bytes} byte limit"
        return current, None
    except FileNotFoundError:
        return None, "file is missing"
    except OSError as exc:
        return None, f"could not inspect file: {exc}"


def _encoded_secret_variants(secret: str) -> set[bytes]:
    raw = secret.encode("utf-8")
    variants = {
        secret.encode("utf-16-le"),
        secret.encode("utf-16-be"),
        secret.encode("utf-32-le"),
        secret.encode("utf-32-be"),
    }
    if len(raw) >= 8:
        variants.update(_base64_secret_variants(secret))
        variants.update(_base32_secret_variants(secret))
        variants.update(_base85_secret_variants(secret))
        variants.update(_character_escape_secret_variants(secret))
        variants.update({raw.hex().encode("ascii"), raw.hex().upper().encode("ascii")})
    return {value for value in variants if value}


def _base64_secret_variants(secret: str) -> set[bytes]:
    raw = secret.encode("utf-8")
    if len(raw) < 8:
        return set()
    variants = {base64.b64encode(raw), base64.urlsafe_b64encode(raw)}
    variants.update(value.rstrip(b"=") for value in tuple(variants))
    return {value for value in variants if value}


def _base32_secret_variants(secret: str) -> set[bytes]:
    raw = secret.encode("utf-8")
    if len(raw) < 8:
        return set()
    variants = {base64.b32encode(raw), base64.b32hexencode(raw)}
    variants.update(value.lower() for value in tuple(variants))
    variants.update(value.rstrip(b"=") for value in tuple(variants))
    return {value for value in variants if value}


def _base85_secret_variants(secret: str) -> set[bytes]:
    raw = secret.encode("utf-8")
    if len(raw) < 8:
        return set()
    variants = {
        base64.b85encode(raw),
        base64.b85encode(raw, pad=True),
        base64.a85encode(raw),
        base64.a85encode(raw, adobe=True),
        base64.a85encode(raw, pad=True),
        base64.a85encode(raw, adobe=True, pad=True),
    }
    return {value for value in variants if value}


def _character_escape_secret_variants(secret: str) -> set[bytes]:
    raw = secret.encode("utf-8")
    if len(raw) < 8:
        return set()
    codepoints = tuple(ord(character) for character in secret)
    variants = {
        "".join(f"\\x{value:02x}" for value in raw),
        "".join(f"%{value:02x}" for value in raw),
        "".join(f"\\{value:03o}" for value in raw),
        "".join(f"&#{value};" for value in codepoints),
        "".join(f"&#x{value:x};" for value in codepoints),
        "".join(f"\\u{{{value:x}}}" for value in codepoints),
        "".join(f"\\U{value:08x}" for value in codepoints),
    }
    if all(value <= 0xffff for value in codepoints):
        variants.add("".join(f"\\u{value:04x}" for value in codepoints))
    return {value.encode("ascii") for value in variants if value}


def _decode_character_escapes(raw_value: bytes) -> bytes:
    def replace(match: re.Match[bytes]) -> bytes:
        groups = match.groups()
        for index in (0, 1, 2, 6, 7):
            if groups[index] is None:
                continue
            base = 10 if index == 6 else 16
            try:
                return chr(int(groups[index], base)).encode("utf-8")
            except (UnicodeEncodeError, ValueError):
                return match.group(0)
        for index, base in ((3, 16), (4, 8), (5, 16)):
            if groups[index] is not None:
                return bytes((int(groups[index], base),))
        return match.group(0)

    decoded = raw_value
    for _ in range(4):
        updated = CHARACTER_ESCAPE_RE.sub(replace, decoded)
        if updated == decoded:
            break
        decoded = updated
    return decoded


def _contains_encoded_secret_bytes(raw_value: bytes, secrets: Iterable[str]) -> bool:
    if b"\0" in raw_value:
        return True
    lowered = raw_value.lower()
    compact_whitespace = re.sub(rb"[\t\n\v\f\r ]+", b"", raw_value)
    compact_lowered = compact_whitespace.lower()
    decoded_escapes = _decode_character_escapes(raw_value)
    decoded_compact_escapes = _decode_character_escapes(compact_whitespace)
    decoded_escapes_changed = (
        decoded_escapes != raw_value
        or decoded_compact_escapes != compact_whitespace
    )
    compact_decoded_escapes = re.sub(rb"[\t\n\v\f\r ]+", b"", decoded_escapes)
    for secret in (str(value) for value in secrets if value):
        if any(encoded in raw_value for encoded in _encoded_secret_variants(secret)):
            return True
        if any(encoded in compact_whitespace for encoded in _base64_secret_variants(secret)):
            return True
        if any(
            encoded.lower() in candidate
            for encoded in _base32_secret_variants(secret)
            for candidate in (lowered, compact_lowered)
        ):
            return True
        if any(
            encoded in compact_whitespace
            for encoded in _base85_secret_variants(secret)
        ):
            return True
        if any(
            encoded.lower() in candidate
            for encoded in _character_escape_secret_variants(secret)
            for candidate in (lowered, compact_lowered)
        ):
            return True
        raw_secret = secret.encode("utf-8")
        if decoded_escapes_changed and len(raw_secret) >= 8 and any(
            raw_secret in candidate
            for candidate in (
                decoded_escapes,
                compact_decoded_escapes,
                decoded_compact_escapes,
            )
        ):
            return True
        if (
            len(raw_secret) >= 8
            and raw_secret not in raw_value
            and raw_secret in compact_whitespace
        ):
            return True
        if (
            len(raw_secret) >= 8
            and raw_secret not in raw_value
            and re.fullmatch(rb"[0-9a-fA-F]+", raw_secret)
            and raw_secret.lower() in compact_lowered
        ):
            return True
        hexadecimal = raw_secret.hex().encode("ascii")
        if len(raw_secret) >= 8 and any(
            hexadecimal in candidate for candidate in (lowered, compact_lowered)
        ):
            return True
    return False


def _contains_secret_path(rel: str, secrets: Iterable[str]) -> bool:
    candidate = str(rel)
    joined_components = re.sub(r"[\\/]+", "", candidate)
    joined_bytes = joined_components.encode("utf-8", errors="replace")
    for secret in (str(value) for value in secrets if value):
        if secret in candidate or secret in joined_components:
            return True
        for encoded in _encoded_secret_variants(secret):
            normalized = encoded.replace(b"/", b"").replace(b"\\", b"")
            if normalized and normalized in joined_bytes:
                return True
    return _contains_encoded_secret_bytes(
        candidate.encode("utf-8", errors="replace"), secrets
    ) or _contains_encoded_secret_bytes(joined_bytes, secrets)


def _sanitize_snapshot_git_metadata(workspace: Path) -> None:
    git_dir = workspace / ".git"
    try:
        git_info = git_dir.lstat()
    except OSError as exc:
        raise RuntimeError(f"could not verify snapshot Git metadata: {exc}") from exc
    if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode):
        raise RuntimeError("snapshot Git metadata is not a trusted directory")
    config = git_dir / "config"
    info_dir = git_dir / "info"
    attributes = info_dir / "attributes"
    try:
        if _path_lexists(config):
            discard_path_verified(config)
        if _path_lexists(info_dir):
            discard_path_verified(info_dir)
        info_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        with config.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "[core]\n"
                "\trepositoryformatversion = 0\n"
                "\tfilemode = true\n"
                "\tbare = false\n"
                "\tlogallrefupdates = true\n"
            )
        config.chmod(0o600)
        with attributes.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "* -text -crlf -ident -filter !working-tree-encoding !diff\n"
            )
        attributes.chmod(0o600)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("could not sanitize snapshot Git metadata") from exc


def _is_harness_runtime_path(rel: str) -> bool:
    parts = Path(str(rel or "")).parts
    if not parts:
        return False
    return parts[0] in RESERVED_PARTS or parts == ("agent.config.json",)


def _snapshot_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("workspace snapshot time budget exhausted")
    return max(0.001, remaining)


@contextlib.contextmanager
def _independent_snapshot_index(
    workspace: Path,
    baseline: str,
    artifacts: Path,
    secrets: Iterable[str],
    deadline: float,
):
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        workspace_info = workspace.lstat()
        git_dir = workspace / ".git"
        git_info = git_dir.lstat()
    except OSError as exc:
        raise RuntimeError(f"could not verify snapshot repository structure: {exc}") from exc
    if (stat.S_ISLNK(workspace_info.st_mode) or not stat.S_ISDIR(workspace_info.st_mode)
            or stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode)):
        raise RuntimeError("snapshot repository structure is not trusted")
    index_path = artifacts / f".snapshot-index-{uuid.uuid4().hex}"
    index_env = {
        "GIT_DIR": str(git_dir),
        "GIT_INDEX_FILE": str(index_path),
        "GIT_WORK_TREE": str(workspace),
    }
    try:
        _required_git(
            ["read-tree", baseline],
            cwd=workspace,
            secrets=secrets,
            extra_env=index_env,
            timeout_sec=_snapshot_timeout(deadline),
        )
        yield index_env
    finally:
        try:
            index_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError("could not discard independent snapshot index") from exc


def workspace_snapshot(workspace: Path, baseline: str, artifacts: Path,
                       *, secrets: Iterable[str] = ()) -> dict[str, Any]:
    secrets = tuple(str(secret) for secret in secrets if secret)
    deadline = time.monotonic() + MAX_SNAPSHOT_SECONDS
    _sanitize_snapshot_git_metadata(workspace)
    _required_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=workspace,
        path_output=True,
        secrets=secrets,
        timeout_sec=_snapshot_timeout(deadline),
    )
    with _independent_snapshot_index(workspace, baseline, artifacts, secrets, deadline) as index_env:
        tracked = _required_git(
            ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", baseline],
            cwd=workspace,
            path_output=True,
            secrets=secrets,
            extra_env=index_env,
            timeout_sec=_snapshot_timeout(deadline),
        )
        untracked = _required_git(
            ["ls-files", "-z", "--others"],
            cwd=workspace,
            path_output=True,
            secrets=secrets,
            extra_env=index_env,
            timeout_sec=_snapshot_timeout(deadline),
        )
        tracked_diff = _required_git(
            ["diff", "--no-ext-diff", "--no-textconv", baseline],
            cwd=workspace,
            secrets=secrets,
            extra_env=index_env,
            timeout_sec=_snapshot_timeout(deadline),
        )["stdout"].removesuffix("\n")
    head = _required_git(
        ["rev-parse", "HEAD"],
        cwd=workspace,
        secrets=secrets,
        timeout_sec=_snapshot_timeout(deadline),
    )
    tracked_files = _parse_nul_paths(tracked["stdout"])
    raw_untracked_files = [
        rel for rel in _parse_nul_paths(untracked["stdout"])
        if not _is_harness_runtime_path(rel)
    ]
    all_changed_paths = tracked_files + raw_untracked_files
    fragmented_path_indexes = _fragmented_secret_indexes(
        [rel.encode("utf-8", errors="replace") for rel in all_changed_paths],
        secrets,
    )
    fragmented_paths = {
        all_changed_paths[index] for index in fragmented_path_indexes
    }
    protected_tracked = {
        rel
        for rel in tracked_files
        if rel in fragmented_paths or _contains_secret_path(rel, secrets)
    }
    protected_untracked = {
        rel
        for rel in raw_untracked_files
        if rel in fragmented_paths or _contains_secret_path(rel, secrets)
    }
    protected_paths = protected_tracked | protected_untracked
    tracked_files = [rel for rel in tracked_files if rel not in protected_paths]
    untracked_files = [rel for rel in raw_untracked_files if rel not in protected_paths]
    safe_changed = sorted(set(tracked_files) | set(untracked_files))
    changed = safe_changed + (["(redacted)"] if protected_paths else [])
    if len(safe_changed) + len(protected_paths) > MAX_SNAPSHOT_CHANGED_FILES:
        raise RuntimeError(
            f"workspace snapshot exceeds {MAX_SNAPSHOT_CHANGED_FILES} changed-file limit"
        )
    pieces = [tracked_diff] if tracked_diff and not protected_tracked else []
    diff_chars = len(tracked_diff) if pieces else 0
    evidence_omissions: list[dict[str, str]] = []
    if protected_paths:
        evidence_omissions.append({
            "path": "(redacted)",
            "reason": "path contains a protected secret encoding",
        })
    if protected_tracked:
        evidence_omissions.append({
            "path": "final.diff",
            "reason": "tracked diff omitted because a path contains a protected secret encoding",
        })
    aggregate_file_bytes = 0
    for rel in safe_changed:
        _snapshot_timeout(deadline)
        path, error = _workspace_regular_file(
            workspace, rel, max_bytes=MAX_FIXTURE_FILE_BYTES
        )
        if error or path is None:
            continue
        aggregate_file_bytes += path.stat().st_size
        if aggregate_file_bytes > MAX_SNAPSHOT_FILE_BYTES:
            raise RuntimeError(
                f"workspace snapshot exceeds {MAX_SNAPSHOT_FILE_BYTES} byte file-evidence limit"
            )
    for rel in untracked_files:
        _snapshot_timeout(deadline)
        path, error = _workspace_regular_file(workspace, rel, max_bytes=MAX_FIXTURE_FILE_BYTES)
        if error or path is None:
            evidence_omissions.append({"path": redact_text(rel, secrets), "reason": error or "unsafe file"})
            continue
        patch = git(["diff", "--no-index", "--no-ext-diff", "--no-textconv", "--", "/dev/null", rel],
                    cwd=workspace, evidence=True, secrets=secrets, include_raw_output=True,
                    timeout_sec=_snapshot_timeout(deadline))
        _require_result(
            patch,
            f"git diff --no-index {rel}",
            allowed_returncodes=(0, 1),
            secrets=secrets,
        )
        patch["stdout"] = patch.pop("_raw_stdout", patch["stdout"])
        patch["stderr"] = patch.pop("_raw_stderr", patch["stderr"])
        patch_text = patch["stdout"].removesuffix("\n")
        if patch_text:
            diff_chars += len(patch_text) + (1 if pieces else 0)
            if diff_chars > MAX_SNAPSHOT_DIFF_CHARS:
                raise RuntimeError(
                    f"workspace snapshot exceeds {MAX_SNAPSHOT_DIFF_CHARS} character diff limit"
                )
            pieces.append(patch_text)
    diff_text = "\n".join(v for v in pieces if v)
    retained_diff = diff_text + ("\n" if diff_text else "")
    (artifacts / "final.diff").write_text(retained_diff, encoding="utf-8")
    final_files = artifacts / "final-files"
    final_file_omissions: list[dict[str, str]] = []
    for rel in safe_changed:
        _snapshot_timeout(deadline)
        source, error = _workspace_regular_file(workspace, rel, max_bytes=MAX_FIXTURE_FILE_BYTES)
        if error or source is None:
            final_file_omissions.append({"path": redact_text(rel, secrets), "reason": error or "unsafe file"})
            continue
        try:
            target = final_files / safe_rel_path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not retain final file evidence for {redact_text(rel, secrets)}: {exc}") from exc
    return {"base_head": baseline, "final_head": head["stdout"].strip(),
            "dirty": bool(changed), "files_changed": changed,
            "diff_sha256": hashlib.sha256(retained_diff.encode()).hexdigest(), "diff_chars": len(retained_diff),
            "evidence_omissions": evidence_omissions, "final_file_omissions": final_file_omissions,
            "git_metadata_retained": True, "execution_workspace_retained": True}


def refresh_diff_metadata(workspace_info: dict[str, Any], diff_path: Path) -> None:
    data = diff_path.read_bytes() if diff_path.exists() else b""
    workspace_info["diff_sha256"] = hashlib.sha256(data).hexdigest()
    workspace_info["diff_chars"] = len(data.decode("utf-8", errors="replace"))


def build_albatross_env(*, nexus_base_url: str, nexus_token: str, model: str,
                        workspace: Path, home: Path, temp_dir: Path, max_steps: int,
                        allow_mutations: bool = True) -> dict[str, str]:
    env = clean_env(home=home, temp_dir=temp_dir)
    env.update({"BACKEND": "openai", "OPENAI_BASE_URL": nexus_base_url.rstrip("/"),
                "OPENAI_API_KEY": nexus_token, "AGENT_MODEL": model, "WORKSPACE_ROOT": str(workspace),
                "OUTSIDE_WORKSPACE": "deny", "ALBATROSS_NO_WIZARD": "true",
                "ALBATROSS_NO_UPDATE_CHECK": "true", "APPROVAL_POLICY": "always",
                "AGENT_MAX_STEPS": str(max_steps),
                "AGENT_TOOLS": ALBATROSS_TOOLS if allow_mutations else ALBATROSS_READ_ONLY_TOOLS,
                "AGENT_TOOL_SELECTION": "fixed", "WARMUP": "false"})
    return env


def albatross_version(executable: str) -> dict[str, Any]:
    has_sep = os.sep in executable or bool(os.altsep and os.altsep in executable)
    if has_sep:
        resolved = str(Path(executable).expanduser().resolve())
    else:
        found = shutil.which(executable) or ""
        resolved = str(Path(found).resolve()) if found else ""
    if not resolved or not Path(resolved).is_file():
        return {"installed": False, "executable": executable, "version": "", "raw": "albatross unavailable"}
    result = run_process([resolved, "--version"], cwd=Path.cwd(), env=clean_env(), timeout_sec=15,
                         isolate_process_group=sys.platform.startswith("linux"),
                         require_descendant_containment=False)
    text = (result["stdout"] or result["stderr"]).strip()
    match = re.search(r"(?:albatross\s+)?v?(\d+\.\d+\.\d+)", text, re.I)
    return {"installed": bool(result["ok"]), "executable": resolved,
            "version": match.group(1) if match else "", "raw": text[:500]}


def albatross_capabilities(executable: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    help_result = run_process([executable, "--help"], cwd=Path.cwd(), env=clean_env(), timeout_sec=15,
                              isolate_process_group=sys.platform.startswith("linux"),
                              require_descendant_containment=False)
    help_text = help_result["stdout"] + "\n" + help_result["stderr"]
    capabilities = {"one_shot": "--print" in help_text, "external_eval": "--eval" in help_text,
        "json_eval_output": "--json" in help_text, "allow_tools": "--allow-tools" in help_text,
        "chat": None, "streaming": None, "tool_calls": None, "structured_trace": None}
    missing = [name for name in REQUIRED_PROBE_CAPABILITIES if not capabilities.get(name)]
    return help_result, capabilities, missing


def _trace_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError("trace parsing time budget exhausted")


def _trace_files(root: Path, *, deadline: float) -> list[Path]:
    try:
        root_info = root.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return []
    found: list[Path] = []
    pending = [root]
    entry_count = 0
    while pending:
        _trace_deadline(deadline)
        current = pending.pop()
        child_dirs: list[Path] = []
        try:
            scan = os.scandir(current)
        except OSError:
            continue
        with scan:
            for entry in scan:
                _trace_deadline(deadline)
                entry_count += 1
                if entry_count > MAX_TRACE_ENTRIES:
                    raise RuntimeError(f"trace enumeration exceeds {MAX_TRACE_ENTRIES} entry limit")
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    child_dirs.append(path)
                elif (entry.name.endswith(".events.jsonl")
                      and stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)):
                    found.append(path)
                    if len(found) > MAX_TRACE_FILES:
                        raise RuntimeError(f"trace enumeration exceeds {MAX_TRACE_FILES} file limit")
        pending.extend(sorted(child_dirs, reverse=True))
    return sorted(found)


def _session_transcript_files(root: Path, *, deadline: float) -> list[Path]:
    try:
        root_info = root.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return []
    found: list[Path] = []
    pending = [root]
    entry_count = 0
    while pending:
        _trace_deadline(deadline)
        current = pending.pop()
        child_dirs: list[Path] = []
        try:
            scan = os.scandir(current)
        except OSError:
            continue
        with scan:
            for entry in scan:
                _trace_deadline(deadline)
                entry_count += 1
                if entry_count > MAX_TRACE_ENTRIES:
                    raise RuntimeError(
                        f"session transcript enumeration exceeds {MAX_TRACE_ENTRIES} entry limit"
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    child_dirs.append(path)
                elif (
                    entry.name.endswith(".jsonl")
                    and not entry.name.endswith(".events.jsonl")
                    and stat.S_ISREG(info.st_mode)
                    and not stat.S_ISLNK(info.st_mode)
                ):
                    found.append(path)
                    if len(found) > MAX_TRACE_FILES:
                        raise RuntimeError(
                            f"session transcript enumeration exceeds {MAX_TRACE_FILES} file limit"
                        )
        pending.extend(sorted(child_dirs, reverse=True))
    return sorted(found)


def parse_session_transcripts(
    session_root: Path,
    *,
    artifact_dir: Path | None = None,
    secrets: Iterable[str] = (),
    deadline: float | None = None,
    max_agent_steps: int = MAX_TRACE_AGENT_STEPS,
    max_files: int = MAX_TRACE_FILES,
    max_total_bytes: int = MAX_TRACE_TOTAL_BYTES,
) -> dict[str, Any]:
    """Normalize trusted Albatross session JSONL when one-shot event logs are absent."""
    if (
        isinstance(max_agent_steps, bool)
        or not isinstance(max_agent_steps, int)
        or not 1 <= max_agent_steps <= MAX_TRACE_AGENT_STEPS
    ):
        raise ValueError(
            f"max_agent_steps must be between 1 and {MAX_TRACE_AGENT_STEPS}"
        )
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or not 0 <= max_files <= MAX_TRACE_FILES
    ):
        raise ValueError(f"max_files must be between 0 and {MAX_TRACE_FILES}")
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or not 0 <= max_total_bytes <= MAX_TRACE_TOTAL_BYTES
    ):
        raise ValueError(
            "max_total_bytes must be between 0 and "
            f"{MAX_TRACE_TOTAL_BYTES}"
        )
    deadline = (
        time.monotonic() + MAX_TRACE_PARSE_SECONDS
        if deadline is None
        else deadline
    )
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    tools: list[str] = []
    turns = 0
    steps = 0
    malformed = 0
    files: list[str] = []
    omissions: list[dict[str, Any]] = []
    total_input_bytes = 0
    transcript_index = 0
    for candidate_index, path in enumerate(
        _session_transcript_files(session_root, deadline=deadline)
    ):
        _trace_deadline(deadline)
        candidate_label = f"session:{candidate_index}"
        if len(files) >= max_files:
            omissions.append(
                {
                    "trace": candidate_label,
                    "reason": "session transcript file budget exhausted",
                }
            )
            continue
        try:
            transcript_size = path.lstat().st_size
        except OSError as exc:
            omissions.append(
                {
                    "trace": candidate_label,
                    "reason": f"could not stat session transcript: {exc}",
                }
            )
            continue
        if transcript_size > MAX_TRACE_FILE_BYTES:
            omissions.append(
                {
                    "trace": candidate_label,
                    "reason": (
                        f"session transcript exceeds {MAX_TRACE_FILE_BYTES} "
                        "byte per-file limit"
                    ),
                }
            )
            continue
        if total_input_bytes + transcript_size > max_total_bytes:
            omissions.append(
                {
                    "trace": candidate_label,
                    "reason": (
                        f"session transcript budget exceeds {max_total_bytes} "
                        "byte aggregate limit"
                    ),
                }
            )
            continue
        try:
            source_handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            omissions.append(
                {
                    "trace": candidate_label,
                    "reason": f"could not read session transcript: {exc}",
                }
            )
            continue
        total_input_bytes += transcript_size
        candidate_steps = 0
        candidate_malformed = 0
        pending_calls: dict[str, str] = {}
        seen_call_ids: set[str] = set()
        completed_tools: list[str] = []
        source_error: OSError | None = None
        try:
            while True:
                _trace_deadline(deadline)
                try:
                    line = source_handle.readline()
                except OSError as exc:
                    source_error = exc
                    break
                if line == "":
                    break
                try:
                    item = json.loads(line)
                except Exception:
                    candidate_malformed += 1
                    continue
                message = item.get("message") if isinstance(item, dict) else None
                if not isinstance(message, dict):
                    candidate_malformed += 1
                    continue
                role = message.get("role")
                if role == "assistant":
                    if candidate_steps >= max_agent_steps - steps:
                        candidate_malformed += 1
                        continue
                    candidate_steps += 1
                    raw_calls = message.get("tool_calls", [])
                    if not isinstance(raw_calls, list):
                        candidate_malformed += 1
                        continue
                    for raw_call in raw_calls:
                        function = (
                            raw_call.get("function")
                            if isinstance(raw_call, dict)
                            else None
                        )
                        call_id = raw_call.get("id") if isinstance(raw_call, dict) else None
                        name = function.get("name") if isinstance(function, dict) else None
                        if (
                            not isinstance(call_id, str)
                            or not call_id
                            or len(call_id) > 1024
                            or call_id in seen_call_ids
                            or raw_call.get("type") != "function"
                            or not isinstance(name, str)
                            or not name
                            or len(name) > 256
                            or not isinstance(function.get("arguments"), str)
                        ):
                            candidate_malformed += 1
                            continue
                        if len(seen_call_ids) >= MAX_TRACE_ENTRIES:
                            raise RuntimeError(
                                "session transcript tool calls exceed "
                                f"{MAX_TRACE_ENTRIES} entry limit"
                            )
                        seen_call_ids.add(call_id)
                        pending_calls[call_id] = name
                elif role == "tool":
                    call_id = message.get("tool_call_id")
                    if (
                        not isinstance(call_id, str)
                        or call_id not in pending_calls
                        or not isinstance(message.get("content"), str)
                    ):
                        candidate_malformed += 1
                        continue
                    completed_tools.append(pending_calls.pop(call_id))
                elif role not in {"system", "user"}:
                    candidate_malformed += 1
        finally:
            try:
                source_handle.close()
            except OSError as exc:
                source_error = source_error or exc
        if source_error is not None:
            total_input_bytes -= transcript_size
            omissions.append(
                {
                    "trace": candidate_label,
                    "reason": f"could not read session transcript: {source_error}",
                }
            )
            continue
        if candidate_steps == 0:
            malformed += candidate_malformed
            continue
        turns += 1
        steps += candidate_steps
        malformed += candidate_malformed + len(pending_calls)
        tools.extend(completed_tools)
        if artifact_dir is not None:
            artifact_path = (
                artifact_dir / f"session-{transcript_index:04d}.events.jsonl"
            )
            transcript_index += 1
            normalized: list[dict[str, Any]] = [
                {
                    "turn": 1,
                    "kind": "toolCall",
                    "name": name,
                    "source": "albatross_session_transcript",
                }
                for name in completed_tools
            ]
            normalized.append(
                {
                    "turn": 1,
                    "kind": "turnSummary",
                    "steps": candidate_steps,
                    "source": "albatross_session_transcript",
                }
            )
            normalized = redact_value(normalized, secrets)
            with artifact_path.open("x", encoding="utf-8", newline="\n") as handle:
                for event in normalized:
                    handle.write(
                        json.dumps(event, separators=(",", ":"), sort_keys=True)
                    )
                    handle.write("\n")
            files.append(str(artifact_path))
        else:
            files.append(str(path))
    tools = _redact_fragmented_value(tools, secrets, fields=None)
    return {
        "tool_calls": tools,
        "tool_call_count": len(tools),
        "agent_turns": turns,
        "agent_steps": steps,
        "context_resets": 0,
        "malformed_trace_lines": malformed,
        "trace_files": files,
        "trace_omissions": omissions,
        "trace_input_bytes": total_input_bytes,
    }


def parse_trace(session_roots: Path | Iterable[Path], *, artifact_dir: Path | None = None,
                secrets: Iterable[str] = (), deadline: float | None = None,
                max_agent_steps: int = MAX_TRACE_AGENT_STEPS) -> dict[str, Any]:
    if (isinstance(max_agent_steps, bool) or not isinstance(max_agent_steps, int)
            or not 1 <= max_agent_steps <= MAX_TRACE_AGENT_STEPS):
        raise ValueError(
            f"max_agent_steps must be between 1 and {MAX_TRACE_AGENT_STEPS}"
        )
    deadline = time.monotonic() + MAX_TRACE_PARSE_SECONDS if deadline is None else deadline
    roots = [session_roots] if isinstance(session_roots, Path) else list(session_roots)
    tools, turns, steps, resets, malformed, files = [], set(), 0, 0, 0, []
    summarized_turns: set[tuple[str, int]] = set()
    omissions: list[dict[str, Any]] = []
    retained_trace_items: list[list[dict[str, Any]]] = []
    total_trace_bytes = 0
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    trace_index = 0
    candidate_index = 0
    for root_index, root in enumerate(roots):
        for path in _trace_files(root, deadline=deadline):
            _trace_deadline(deadline)
            candidate_label = f"{root_index}:{candidate_index}"
            candidate_index += 1
            try:
                trace_size = path.lstat().st_size
            except OSError as exc:
                omissions.append({"trace": candidate_label, "reason": f"could not stat trace: {exc}"})
                continue
            if trace_size > MAX_TRACE_FILE_BYTES:
                omissions.append({"trace": candidate_label,
                                  "reason": f"trace exceeds {MAX_TRACE_FILE_BYTES} byte per-file limit"})
                continue
            if total_trace_bytes + trace_size > MAX_TRACE_TOTAL_BYTES:
                omissions.append({"trace": candidate_label,
                                  "reason": f"trace budget exceeds {MAX_TRACE_TOTAL_BYTES} byte aggregate limit"})
                continue
            total_trace_bytes += trace_size
            try:
                source_handle = path.open("r", encoding="utf-8", errors="replace")
            except OSError as exc:
                total_trace_bytes -= trace_size
                omissions.append({"trace": candidate_label, "reason": f"could not read trace: {exc}"})
                continue
            dest: Path | None = None
            dest_handle = None
            source_error: OSError | None = None
            retained_items: list[dict[str, Any]] = []
            tool_count_before = len(tools)
            turns_before = set(turns)
            summarized_turns_before = set(summarized_turns)
            steps_before, resets_before, malformed_before = steps, resets, malformed
            try:
                if artifact_dir is not None:
                    dest = artifact_dir / f"{root_index}-{trace_index:04d}.events.jsonl"
                    trace_index += 1
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest_handle = dest.open("w", encoding="utf-8", newline="\n")
                    files.append(str(dest))
                else:
                    files.append(str(path))
                while True:
                    _trace_deadline(deadline)
                    try:
                        line = source_handle.readline()
                    except OSError as exc:
                        source_error = exc
                        break
                    if line == "":
                        break
                    try:
                        item = json.loads(line)
                    except Exception:
                        malformed += 1
                        continue
                    if not isinstance(item, dict):
                        malformed += 1
                        continue
                    if dest_handle is not None:
                        retained_item = redact_value(item, secrets)
                        retained_items.append(retained_item)
                        dest_handle.write(json.dumps(retained_item, separators=(",", ":"), sort_keys=True))
                        dest_handle.write("\n")
                    raw_turn = item.get("turn")
                    if (isinstance(raw_turn, bool)
                            or not isinstance(raw_turn, int)
                            or not 0 <= raw_turn < 10 ** MAX_TRACE_TURN_DIGITS):
                        malformed += 1
                        continue
                    turns.add((candidate_label, raw_turn))
                    if item.get("kind") == "toolCall" and item.get("name"):
                        tools.append(str(item["name"]))
                    elif item.get("kind") == "contextCompacted":
                        resets += 1
                    elif item.get("kind") == "turnSummary":
                        summary_key = (candidate_label, raw_turn)
                        if summary_key in summarized_turns:
                            malformed += 1
                            continue
                        raw_steps = item.get("steps")
                        if raw_steps is None:
                            parsed_steps = 0
                        elif isinstance(raw_steps, bool):
                            malformed += 1
                            continue
                        elif (isinstance(raw_steps, int)
                              and 0 <= raw_steps < 10 ** MAX_TRACE_STEP_DIGITS):
                            parsed_steps = raw_steps
                        elif (isinstance(raw_steps, str)
                              and len(raw_steps) <= MAX_TRACE_STEP_DIGITS
                              and re.fullmatch(r"\d+", raw_steps)):
                            parsed_steps = int(raw_steps)
                        else:
                            malformed += 1
                            continue
                        summarized_turns.add(summary_key)
                        if parsed_steps > max_agent_steps - steps:
                            malformed += 1
                            continue
                        steps += parsed_steps
            finally:
                try:
                    source_handle.close()
                except OSError as exc:
                    source_error = source_error or exc
                if dest_handle is not None:
                    dest_handle.close()
            if source_error is not None:
                del tools[tool_count_before:]
                turns = turns_before
                summarized_turns = summarized_turns_before
                steps, resets, malformed = steps_before, resets_before, malformed_before
                total_trace_bytes -= trace_size
                omissions.append({"trace": candidate_label, "reason": f"could not read trace: {source_error}"})
                retained_path = str(dest if dest is not None else path)
                if retained_path in files:
                    files.remove(retained_path)
                if dest is not None:
                    dest.unlink(missing_ok=True)
            elif dest is not None:
                retained_trace_items.append(retained_items)
    if artifact_dir is not None:
        retained_trace_items = _redact_fragmented_value(
            retained_trace_items, secrets, fields=None, include_keys=True
        )
        for retained_path, retained_items in zip(files, retained_trace_items):
            with Path(retained_path).open("w", encoding="utf-8", newline="\n") as handle:
                for retained_item in retained_items:
                    handle.write(
                        json.dumps(retained_item, separators=(",", ":"), sort_keys=True)
                    )
                    handle.write("\n")
    tools = _redact_fragmented_value(tools, secrets, fields=None)
    return {"tool_calls": tools, "tool_call_count": len(tools), "agent_turns": len(turns),
            "agent_steps": steps, "context_resets": resets, "malformed_trace_lines": malformed,
            "trace_files": files, "trace_omissions": omissions, "trace_input_bytes": total_trace_bytes}


def run_validation(fixture: dict[str, Any], workspace: Path, home: Path, temp_dir: Path,
                   *, deadline: float, secrets: Iterable[str] = ()) -> dict[str, Any]:
    commands = fixture.get("expected", {}).get("validation") or []
    if commands:
        discard_path_verified(temp_dir)
        _mkdir_private(temp_dir)
    results: list[dict[str, Any]] = []
    budget_exhausted = False
    timed_out = False
    for argv in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"argv": argv, "ok": False, "returncode": None, "timed_out": True,
                            "stdout": "", "stderr": "validation time budget exhausted before command",
                            "duration_ms": 0.0, "launch_error": None, "output_truncated": False})
            budget_exhausted = True
            timed_out = True
            break
        timeout_sec = remaining
        try:
            sandbox_argv, sandbox_env = _validation_sandbox_argv(
                [str(v) for v in argv], workspace, home, temp_dir
            )
        except RuntimeError as exc:
            result = {
                "ok": False,
                "returncode": None,
                "timed_out": False,
                "stdout": "",
                "stderr": redact_text(str(exc), secrets),
                "duration_ms": 0.0,
                "launch_error": "validation_sandbox_unavailable",
                "output_truncated": False,
            }
            results.append({"argv": argv, **result})
            break
        result = run_process(
            sandbox_argv,
            cwd=workspace,
            env=sandbox_env,
            timeout_sec=timeout_sec,
            secrets=secrets,
            isolate_process_group=True,
            scratch_dir=temp_dir,
            scratch_max_bytes=MAX_VALIDATION_SCRATCH_BYTES,
            scratch_max_entries=MAX_VALIDATION_SCRATCH_ENTRIES,
            aggregate_memory_max_bytes=MAX_VALIDATION_AGGREGATE_MEMORY_BYTES,
        )
        results.append({"argv": argv, **result})
        if result["timed_out"]:
            timed_out = True
            budget_exhausted = time.monotonic() >= deadline
            break
    passed = None if not commands else bool(len(results) == len(commands) and all(v["ok"] for v in results))
    return {"commands": results, "passed": passed, "budget_exhausted": budget_exhausted,
            "timed_out": timed_out}


def _objective_checks_with_reader(
    fixture: dict[str, Any],
    changed: list[str],
    validation: dict[str, Any],
    read_content: Any,
) -> dict[str, Any]:
    expected, checks = fixture.get("expected", {}), []
    content_cache: dict[str, tuple[str | None, str | None]] = {}
    if "files_changed" in expected:
        wanted = sorted(str(v) for v in expected["files_changed"])
        checks.append({"kind": "files_changed", "passed": sorted(changed) == wanted,
                       "expected": wanted, "actual": sorted(changed)})
    if "allowed_files_changed" in expected:
        allowed = {str(v) for v in expected["allowed_files_changed"]}
        checks.append({"kind": "allowed_files_changed", "passed": set(changed) <= allowed,
                       "allowed": sorted(allowed), "actual": sorted(changed)})
    for key, negate in (("file_contains", False), ("file_not_contains", True)):
        for spec in expected.get(key) or []:
            rel = str(spec["path"])
            if rel not in content_cache:
                content_cache[rel] = read_content(rel)
            text, error = content_cache[rel]
            if error or text is None:
                checks.append({"kind": key, "path": spec["path"], "needle": spec["needle"],
                               "passed": False, "error": error or "unsafe file"})
                continue
            found = str(spec["needle"]) in text
            checks.append({"kind": key, "path": spec["path"], "needle": spec["needle"],
                           "passed": (not found) if negate else found})
    if validation.get("passed") is not None:
        checks.append({"kind": "validation", "passed": bool(validation["passed"])})
    return {"passed": None if not checks else all(v["passed"] for v in checks), "checks": checks}


def objective_checks(fixture: dict[str, Any], workspace: Path, changed: list[str], validation: dict[str, Any]) -> dict[str, Any]:
    def read_content(rel: str) -> tuple[str | None, str | None]:
        path, error = _workspace_regular_file(
            workspace, rel, max_bytes=MAX_OBJECTIVE_FILE_BYTES
        )
        text: str | None = None
        if error == f"file exceeds {MAX_OBJECTIVE_FILE_BYTES} byte limit":
            error = f"file exceeds {MAX_OBJECTIVE_FILE_BYTES} byte objective-read limit"
        if error is None and path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                error = f"could not read file: {exc}"
        elif error is None:
            error = "unsafe file"
        return text, error

    return _objective_checks_with_reader(fixture, changed, validation, read_content)


def _contains_ordered_secret_fragments(
    value: bytes,
    protected: bytes,
    *,
    min_fragment_bytes: int,
) -> bool:
    minimum = max(1, min_fragment_bytes)
    if len(protected) < minimum * 2:
        return False
    states = {(0, 0, 0)}
    visited: set[tuple[int, int, int]] = set()
    transitions = 0
    while states:
        secret_position, value_position, fragment_count = states.pop()
        state = (secret_position, value_position, fragment_count)
        if state in visited:
            continue
        visited.add(state)
        if len(visited) > MAX_FRAGMENT_SEARCH_STATES:
            return True
        needle = protected[secret_position : secret_position + minimum]
        if len(needle) < minimum:
            continue
        match_start = value.find(needle, value_position)
        while match_start >= 0:
            transitions += 1
            if transitions > MAX_FRAGMENT_SEARCH_STATES:
                return True
            match_length = minimum
            limit = min(len(protected) - secret_position, len(value) - match_start)
            while (
                match_length < limit
                and protected[secret_position + match_length]
                == value[match_start + match_length]
            ):
                match_length += 1
            next_secret_position = secret_position + match_length
            next_fragment_count = min(2, fragment_count + 1)
            if (
                next_secret_position == len(protected)
                and next_fragment_count >= 2
            ):
                return True
            states.add(
                (
                    next_secret_position,
                    match_start + match_length,
                    next_fragment_count,
                )
            )
            match_start = value.find(needle, match_start + 1)
    return False


def _contains_unordered_secret_fragments(
    value: bytes,
    protected: bytes,
    *,
    min_fragment_bytes: int,
) -> bool:
    minimum = max(MIN_UNORDERED_FRAGMENT_BYTES, min_fragment_bytes)
    if len(protected) < minimum * 2:
        return False
    states: set[tuple[int, tuple[tuple[int, int], ...], int]] = {(0, (), 0)}
    visited: set[tuple[int, tuple[tuple[int, int], ...], int]] = set()
    transitions = 0
    while states:
        secret_position, used_intervals, fragment_count = states.pop()
        state = (secret_position, used_intervals, fragment_count)
        if state in visited:
            continue
        visited.add(state)
        if len(visited) > MAX_FRAGMENT_SEARCH_STATES:
            return True
        needle = protected[secret_position : secret_position + minimum]
        if len(needle) < minimum:
            continue
        match_start = value.find(needle)
        while match_start >= 0:
            transitions += 1
            if transitions > MAX_FRAGMENT_SEARCH_STATES:
                return True
            match_length = minimum
            limit = min(len(protected) - secret_position, len(value) - match_start)
            while (
                match_length < limit
                and protected[secret_position + match_length]
                == value[match_start + match_length]
            ):
                match_length += 1
            match_end = match_start + match_length
            if any(
                match_start < used_end and used_start < match_end
                for used_start, used_end in used_intervals
            ):
                match_start = value.find(needle, match_start + 1)
                continue
            next_secret_position = secret_position + match_length
            next_fragment_count = min(2, fragment_count + 1)
            if (
                next_secret_position == len(protected)
                and next_fragment_count >= 2
            ):
                return True
            states.add(
                (
                    next_secret_position,
                    tuple(sorted((*used_intervals, (match_start, match_end)))),
                    next_fragment_count,
                )
            )
            match_start = value.find(needle, match_start + 1)
    return False


def _fragmented_secret_indexes(
    values: list[bytes],
    secrets: Iterable[str],
    *,
    whole_values: bool = False,
    embedded_raw_only: bool = False,
    min_fragment_bytes: int = 1,
) -> set[int]:
    all_implicated: set[int] = set()
    for secret in (str(value) for value in secrets if value):
        raw_secret = secret.encode("utf-8")
        if len(raw_secret) < 8:
            continue
        casefold_variants = (
            _base32_secret_variants(secret)
            | _character_escape_secret_variants(secret)
        )
        for protected in {raw_secret, *_encoded_secret_variants(secret)}:
            case_insensitive = (
                protected in casefold_variants
                or re.fullmatch(rb"[0-9a-fA-F]+", protected) is not None
            )
            normalized_protected = protected.lower() if case_insensitive else protected
            require_whole_value = whole_values or (
                embedded_raw_only and protected != raw_secret
            )
            normalized_values: list[set[bytes]] = []
            for raw_value in values:
                compact = re.sub(rb"[\t\n\v\f\r ]+", b"", raw_value)
                decoded = _decode_character_escapes(raw_value)
                decoded_compact = _decode_character_escapes(compact)
                views = {
                    raw_value,
                    compact,
                    decoded,
                    decoded_compact,
                    re.sub(rb"[\t\n\v\f\r ]+", b"", decoded),
                }
                if case_insensitive:
                    views = {view.lower() for view in views}
                normalized_values.append(views)
            if not require_whole_value:
                for index, views in enumerate(normalized_values):
                    if any(normalized_protected in view for view in views):
                        continue
                    if any(
                        _contains_ordered_secret_fragments(
                            view,
                            normalized_protected,
                            min_fragment_bytes=min_fragment_bytes,
                        )
                        or _contains_unordered_secret_fragments(
                            view,
                            normalized_protected,
                            min_fragment_bytes=min_fragment_bytes,
                        )
                        for view in views
                    ):
                        all_implicated.add(index)
            candidates = [
                (index, views)
                for index, views in enumerate(normalized_values)
                if not any(normalized_protected in view for view in views)
            ]
            implicated: set[int] = set()
            states: dict[int, set[frozenset[int]]] = {0: {frozenset()}}
            for position in range(len(normalized_protected)):
                current_states = tuple(states.get(position, ()))
                for used in current_states:
                    for index, views in candidates:
                        if index in used:
                            continue
                        end = len(normalized_protected)
                        while (
                            end > position
                            and not any(
                                (
                                    normalized_protected[position:end] == view
                                    if require_whole_value
                                    else normalized_protected[position:end] in view
                                )
                                for view in views
                            )
                        ):
                            end -= 1
                        if end - position < min_fragment_bytes:
                            continue
                        updated = used | {index}
                        if end == len(normalized_protected) and len(updated) > 1:
                            implicated.update(updated)
                            continue
                        end_states = states.setdefault(end, set())
                        if any(existing <= updated for existing in end_states):
                            continue
                        end_states.difference_update(
                            tuple(
                                existing
                                for existing in end_states
                                if updated < existing
                            )
                        )
                        end_states.add(updated)
                        if (
                            sum(len(entries) for entries in states.values())
                            > MAX_FRAGMENT_SEARCH_STATES
                        ):
                            return set(range(len(values)))
            all_implicated.update(implicated)
    return all_implicated


def _redact_fragmented_value(
    value: Any,
    secrets: Iterable[str],
    *,
    fields: frozenset[str] | None = frozenset({"stdout", "stderr"}),
    include_keys: bool = False,
    whole_values: bool = False,
    embedded_raw_only: bool = False,
    min_fragment_bytes: int = 1,
) -> Any:
    leaves: list[str] = []

    def collect(candidate: Any, field: str | None = None) -> None:
        if isinstance(candidate, str) and (fields is None or field in fields):
            leaves.append(candidate)
        elif isinstance(candidate, dict):
            for key, nested in candidate.items():
                if include_keys:
                    leaves.append(str(key))
                collect(nested, str(key))
        elif isinstance(candidate, (list, tuple)):
            for nested in candidate:
                collect(nested, field)

    collect(value)
    implicated = _fragmented_secret_indexes(
        [leaf.encode("utf-8", errors="replace") for leaf in leaves],
        secrets,
        whole_values=whole_values,
        embedded_raw_only=embedded_raw_only,
        min_fragment_bytes=min_fragment_bytes,
    )
    leaf_index = 0

    def rebuild(candidate: Any, field: str | None = None) -> Any:
        nonlocal leaf_index
        if isinstance(candidate, str) and (fields is None or field in fields):
            replacement = "(redacted)" if leaf_index in implicated else candidate
            leaf_index += 1
            return replacement
        if isinstance(candidate, dict):
            rebuilt = {}
            for key, nested in candidate.items():
                rebuilt_key = key
                if include_keys:
                    if leaf_index in implicated:
                        rebuilt_key = f"(redacted-key-{leaf_index})"
                    leaf_index += 1
                rebuilt[rebuilt_key] = rebuild(nested, str(key))
            return rebuilt
        if isinstance(candidate, list):
            return [rebuild(nested, field) for nested in candidate]
        if isinstance(candidate, tuple):
            return tuple(rebuild(nested, field) for nested in candidate)
        return candidate

    return rebuild(value)


def scrub_retained_artifacts(artifacts: Path, secrets: Iterable[str]) -> list[str]:
    secrets = tuple(str(secret) for secret in secrets if secret)
    omitted: list[str] = []
    raw_artifacts: dict[Path, bytes] = {}
    for path in sorted(artifacts.rglob("*")):
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                raw_artifacts[path] = path.read_bytes()
        except OSError:
            continue
    raw_paths: list[Path] = []
    raw_values: list[bytes] = []
    for path, raw_value in raw_artifacts.items():
        raw_paths.extend((path, path))
        raw_values.extend(
            (
                str(path.relative_to(artifacts)).encode("utf-8", errors="replace"),
                raw_value,
            )
        )
    fragmented_indexes = _fragmented_secret_indexes(
        raw_values, secrets
    )
    cross_artifact_secret_paths = {raw_paths[index] for index in fragmented_indexes}
    for path in sorted(artifacts.rglob("*")):
        if _contains_secret_path(str(path.relative_to(artifacts)), secrets):
            try:
                discard_path_verified(path)
                omitted.append("(redacted)")
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("could not discard artifact with encoded secret path") from exc
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            try:
                path.unlink()
                omitted.append(str(path.relative_to(artifacts)))
            except OSError as exc:
                raise RuntimeError(f"could not discard retained symlink artifact: {path}: {exc}") from exc
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if path in cross_artifact_secret_paths:
            try:
                path.unlink()
                omitted.append(str(path.relative_to(artifacts)))
            except OSError as exc:
                raise RuntimeError(
                    "could not discard cross-artifact secret fragment"
                ) from exc
            continue
        try:
            raw_artifact = path.read_bytes()
        except OSError:
            raw_artifact = b""
        if _contains_encoded_secret_bytes(raw_artifact, secrets):
            try:
                path.unlink()
                omitted.append(str(path.relative_to(artifacts)))
            except OSError as exc:
                raise RuntimeError("could not discard encoded or binary retained artifact") from exc
            continue
        fd = -1
        temp: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".nexus-scrub-", suffix=".tmp", dir=str(path.parent), text=True)
            temp = Path(temp_name)
            dest_handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
            fd = -1
            changed = False
            with dest_handle as dest, path.open("r", encoding="utf-8") as source:
                for line in source:
                    clean = redact_text(line, secrets)
                    changed = changed or clean != line
                    dest.write(clean)
            if changed:
                os.replace(temp, path)
                temp = None
        except (UnicodeDecodeError, OSError):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp is not None:
                temp.unlink(missing_ok=True)
                temp = None
            try:
                path.unlink()
                omitted.append(str(path.relative_to(artifacts)))
            except OSError:
                raise RuntimeError(f"could not sanitize or discard retained artifact: {path}")
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp is not None:
                temp.unlink(missing_ok=True)
    return omitted


def _redact_omitted_artifact_paths(
    paths: Iterable[str], secrets: Iterable[str]
) -> list[str]:
    protected = tuple(str(secret) for secret in secrets if secret)
    redacted = redact_value(list(paths), protected)
    fragmented = _redact_fragmented_value(
        redacted,
        protected,
        fields=None,
        min_fragment_bytes=1,
    )
    return [str(path) for path in fragmented]


def mission_prompt(fixture: dict[str, Any], allow_mutations: bool) -> str:
    mode = (
        "You may edit files. No command-execution tool is exposed, so do not attempt "
        "to run tests; the harness will run declared validation after you finish."
        if allow_mutations
        else "This is read-only: do not mutate files or run mutating commands."
    )
    return (f"{fixture['mission'].strip()}\n\n{mode} Work only inside the current repository workspace. "
            "Do not modify .git, .albatross, .small-harness, .sessions, agent.config.json, or files outside the workspace. "
            "Do not commit or push. Use repository tools rather than guessing file contents.")


def _write_albatross_runtime_config(workspace: Path, trace_root: Path) -> None:
    target = workspace / "agent.config.json"
    if _path_lexists(target):
        raise RuntimeError("Albatross runtime config path is not empty")
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({"sessionDir": str(trace_root.resolve())}, handle, indent=2)
            handle.write("\n")
        target.chmod(0o600)
    except OSError as exc:
        raise RuntimeError("could not write Albatross runtime config") from exc


class NexusApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def _nexus_api_url(base_url: str, path: str) -> str:
    root = str(base_url or "").strip().rstrip("/")
    if not root:
        raise ValueError("Nexus base URL is required")
    parsed = urlsplit(root)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Nexus base URL must be an HTTP(S) URL without credentials, query, or fragment")
    suffix = "/" + str(path or "").lstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + suffix, "", ""))


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def nexus_api_request(
    method: str,
    base_url: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    if not token:
        raise NexusApiError("Nexus bearer token is required")
    raw_body = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urlrequest.Request(
        _nexus_api_url(base_url, path),
        data=raw_body,
        headers=headers,
        method=str(method or "GET").upper(),
    )
    try:
        opener = urlrequest.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=max(0.1, float(timeout_sec))) as response:
            raw = response.read(MAX_NEXUS_API_RESPONSE_BYTES + 1)
    except urlerror.HTTPError as exc:
        detail = exc.read(min(MAX_NEXUS_API_RESPONSE_BYTES, 64_000)).decode("utf-8", errors="replace")
        raise NexusApiError(
            redact_text(f"Nexus API HTTP {exc.code}: {detail}", [token]),
            status=int(exc.code),
        ) from exc
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise NexusApiError(redact_text(f"Nexus API request failed: {exc}", [token])) from exc
    if len(raw) > MAX_NEXUS_API_RESPONSE_BYTES:
        raise NexusApiError("Nexus API response exceeded the harness size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NexusApiError("Nexus API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise NexusApiError("Nexus API response must be a JSON object")
    return payload


def _task_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task")
    if not isinstance(task, dict):
        raise NexusApiError("Nexus Coding API response omitted task state")
    return task


def _nexus_agent_status(task: dict[str, Any]) -> str:
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    return str(agent.get("status") or "idle").strip().lower()


def _nexus_agent_run_clock(
    task: dict[str, Any],
    *,
    wall_time_sec: float,
) -> tuple[str, float, float]:
    """Anchor client timing to the server-side agent start, after fixture setup."""
    received_monotonic = time.monotonic()
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    try:
        elapsed_at_receipt = float(agent.get("elapsed_runtime_sec") or 0.0)
    except (TypeError, ValueError):
        elapsed_at_receipt = 0.0
    if not math.isfinite(elapsed_at_receipt) or elapsed_at_receipt < 0:
        elapsed_at_receipt = 0.0
    started_monotonic = received_monotonic - elapsed_at_receipt
    try:
        server_started_at = float(agent.get("started_at"))
        if not server_started_at > 0:
            raise ValueError("invalid server start time")
        started_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(server_started_at),
        )
    except (TypeError, ValueError, OverflowError, OSError):
        started_at = now_iso()
    return (
        started_at,
        started_monotonic,
        started_monotonic + float(wall_time_sec),
    )


def _wait_for_nexus_task(
    task_id: str,
    *,
    base_url: str,
    token: str,
    deadline: float,
) -> tuple[dict[str, Any], bool]:
    last_task: dict[str, Any] = {}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                pause = nexus_api_request(
                    "POST",
                    base_url,
                    f"/coding/tasks/{quote(task_id, safe='')}/agent-pause",
                    token=token,
                    body={},
                    timeout_sec=10.0,
                )
                last_task = _task_from_payload(pause)
            except NexusApiError:
                pass
            settle_deadline = time.monotonic() + 10.0
            while _nexus_agent_status(last_task) not in NEXUS_TERMINAL_STATUSES:
                if time.monotonic() >= settle_deadline:
                    break
                try:
                    payload = nexus_api_request(
                        "GET",
                        base_url,
                        f"/coding/tasks/{quote(task_id, safe='')}",
                        token=token,
                        timeout_sec=2.0,
                    )
                    last_task = _task_from_payload(payload)
                except NexusApiError:
                    break
                time.sleep(0.1)
            return last_task, True
        payload = nexus_api_request(
            "GET",
            base_url,
            f"/coding/tasks/{quote(task_id, safe='')}",
            token=token,
            timeout_sec=min(30.0, max(1.0, remaining)),
        )
        last_task = _task_from_payload(payload)
        if _nexus_agent_status(last_task) in NEXUS_TERMINAL_STATUSES:
            return last_task, False
        time.sleep(min(NEXUS_POLL_INTERVAL_SEC, max(0.05, remaining)))


def _nexus_harness_diff_after_workers(
    task_id: str,
    *,
    base_url: str,
    token: str,
    wait_timeout_sec: float,
) -> tuple[dict[str, Any], float]:
    deadline = time.monotonic() + max(0.1, float(wait_timeout_sec))
    last_error: NexusApiError | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NexusApiError(
                f"Nexus harness evidence remained active: {last_error}"
            )
        attempt_started = time.monotonic()
        try:
            payload = nexus_api_request(
                "GET",
                base_url,
                f"/coding/harness/tasks/{quote(task_id, safe='')}/diff",
                token=token,
                timeout_sec=min(30.0, max(0.1, remaining)),
            )
            return payload, attempt_started
        except NexusApiError as exc:
            last_error = exc
            if exc.status != 409:
                raise
            time.sleep(min(0.25, max(0.01, remaining)))


def _delete_nexus_harness_task(
    task_id: str,
    *,
    base_url: str,
    token: str,
    wait_timeout_sec: float = NEXUS_GUARDED_WORKER_TIMEOUT_SEC,
) -> None:
    last_error: NexusApiError | None = None
    deadline = time.monotonic() + max(0.1, float(wait_timeout_sec))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            payload = nexus_api_request(
                "DELETE",
                base_url,
                f"/coding/harness/tasks/{quote(task_id, safe='')}",
                token=token,
                timeout_sec=min(15.0, max(0.1, remaining)),
            )
            result = payload.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise NexusApiError("Nexus Coding API did not confirm harness task deletion")
            return
        except NexusApiError as exc:
            last_error = exc
            if exc.status != 409:
                break
            time.sleep(min(0.25, max(0.01, remaining)))
    raise NexusApiError(f"could not delete Nexus harness task: {last_error}")


def run_nexus_validation(
    fixture: dict[str, Any],
    task_id: str,
    *,
    base_url: str,
    token: str,
    deadline: float,
) -> dict[str, Any]:
    commands = fixture.get("expected", {}).get("validation") or []
    results: list[dict[str, Any]] = []
    budget_exhausted = False
    timed_out = False
    for raw_argv in commands:
        argv = [str(value) for value in raw_argv]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({
                "argv": argv,
                "ok": False,
                "returncode": None,
                "timed_out": True,
                "stdout": "",
                "stderr": "validation time budget exhausted before command",
                "duration_ms": 0.0,
                "launch_error": None,
                "output_truncated": False,
            })
            budget_exhausted = True
            timed_out = True
            break
        payload = nexus_api_request(
            "POST",
            base_url,
            f"/coding/harness/tasks/{quote(task_id, safe='')}/validation",
            token=token,
            body={"argv": argv, "timeout_sec": remaining},
            timeout_sec=remaining + 5.0,
        )
        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            raise NexusApiError("Nexus validation response omitted command result")
        stderr = redact_text(str(raw_result.get("stderr") or ""), [token])
        command_timed_out = bool(
            raw_result.get("returncode") is None and "timeout" in stderr.lower()
        )
        normalized = {
            "argv": argv,
            "ok": bool(raw_result.get("ok")),
            "returncode": raw_result.get("returncode"),
            "timed_out": command_timed_out,
            "stdout": redact_text(str(raw_result.get("stdout") or ""), [token]),
            "stderr": stderr,
            "duration_ms": float(raw_result.get("duration_ms") or 0.0),
            "launch_error": (
                "command_failed_to_launch"
                if raw_result.get("returncode") is None and not command_timed_out
                else None
            ),
            "output_truncated": bool(
                raw_result.get("stdout_truncated") or raw_result.get("stderr_truncated")
            ),
        }
        results.append(normalized)
        if command_timed_out:
            timed_out = True
            budget_exhausted = time.monotonic() >= deadline
            break
    passed = None if not commands else bool(len(results) == len(commands) and all(item["ok"] for item in results))
    return {
        "commands": results,
        "passed": passed,
        "budget_exhausted": budget_exhausted,
        "timed_out": timed_out,
    }


def _nexus_trajectory(task: dict[str, Any]) -> dict[str, Any]:
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    events = [item for item in (agent.get("events") or []) if isinstance(item, dict)]
    tool_names = [
        str(item.get("name") or "")
        for item in events
        if str(item.get("type") or "") == "tool_finished" and str(item.get("name") or "")
    ]
    route_history: list[dict[str, Any]] = []
    for item in events:
        event_type = str(item.get("type") or "")
        if event_type == "started":
            route_history.append({
                "event": "started",
                "backend": str(item.get("backend") or ""),
                "upstream_model": str(item.get("upstream_model") or ""),
                "reason": str(item.get("route_reason") or ""),
            })
        elif event_type == "semantic_reroute":
            route_history.append({
                "event": "semantic_reroute",
                "backend": str(item.get("backend") or ""),
                "upstream_model": str(item.get("upstream_model") or ""),
                "previous_backend": str(item.get("previous_backend") or ""),
                "previous_upstream_model": str(item.get("previous_upstream_model") or ""),
            })
    return {
        "agent_turns": sum(1 for item in events if str(item.get("type") or "") == "assistant"),
        "agent_steps": int(agent.get("cycle") or 0),
        "tool_calls": len(tool_names),
        "tool_call_names": tool_names,
        "file_read_observed": any(name in {"coding_read_file", "coding_read_file_lines"} for name in tool_names),
        "context_resets": sum(1 for item in events if str(item.get("type") or "") == "context_reset"),
        "malformed_trace_lines": sum(
            1
            for item in events
            if str(item.get("type") or "") == "no_tool_call" and item.get("malformed_text_tool_call") is True
        ),
        "trace_input_bytes": 0,
        "event_window": "public_task_last_80",
        "route_history": route_history,
    }


def _nexus_final_file_reader(
    task_id: str,
    *,
    base_url: str,
    token: str,
    cache: dict[str, tuple[str | None, str | None]],
    non_text_paths: set[str],
    file_modes: dict[str, str],
    deadline: float,
) -> Any:
    aggregate_bytes = 0

    def read_content(rel: str) -> tuple[str | None, str | None]:
        nonlocal aggregate_bytes
        if rel in cache:
            return cache[rel]
        try:
            timeout_sec = min(30.0, _snapshot_timeout(deadline))
            payload = nexus_api_request(
                "GET",
                base_url,
                f"/coding/harness/tasks/{quote(task_id, safe='')}/file?path={quote(rel, safe='')}",
                token=token,
                timeout_sec=timeout_sec,
            )
            response_path = payload.get("path")
            size = payload.get("size")
            encoding = payload.get("encoding")
            mode = payload.get("mode")
            content = payload.get("content")
            if response_path != rel:
                cache[rel] = (None, "file response path did not match the request")
            elif isinstance(size, bool) or not isinstance(size, int) or size < 0:
                cache[rel] = (None, "file response omitted a valid byte size")
            elif size > MAX_OBJECTIVE_FILE_BYTES:
                cache[rel] = (
                    None,
                    f"file exceeds {MAX_OBJECTIVE_FILE_BYTES} byte objective-read limit",
                )
            elif aggregate_bytes + size > MAX_SNAPSHOT_FILE_BYTES:
                raise RuntimeError(
                    f"workspace snapshot exceeds {MAX_SNAPSHOT_FILE_BYTES} byte file-evidence limit"
                )
            elif encoding in {"binary", "symlink"} and content is None:
                aggregate_bytes += size
                non_text_paths.add(rel)
                cache[rel] = (None, f"{encoding} file content omitted")
            elif encoding != "utf-8" or not isinstance(content, str):
                cache[rel] = (None, "file response omitted text content")
            elif mode not in {"100644", "100755"}:
                cache[rel] = (None, "file response omitted a valid Git file mode")
            elif len(content.encode("utf-8")) != size:
                cache[rel] = (None, "file response byte size did not match its text content")
            else:
                aggregate_bytes += size
                file_modes[rel] = mode
                cache[rel] = (content, None)
        except NexusApiError as exc:
            cache[rel] = (None, str(exc))
        return cache[rel]

    return read_content


def _untracked_text_diff(
    rel: str,
    content: str,
    mode: str,
    *,
    deadline: float,
) -> str:
    """Render an untracked patch through the same Git operation as Albatross."""
    if mode not in {"100644", "100755"}:
        raise ValueError(f"unsupported untracked Git file mode: {mode}")
    safe = safe_rel_path(rel)
    with tempfile.TemporaryDirectory(prefix="nexus-harness-untracked-") as raw_root:
        root = Path(raw_root)
        target = root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        target.chmod(0o755 if mode == "100755" else 0o644)
        patch = git(
            [
                "diff",
                "--no-index",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                "/dev/null",
                safe.as_posix(),
            ],
            cwd=root,
            evidence=True,
            include_raw_output=True,
            timeout_sec=_snapshot_timeout(deadline),
        )
        _require_result(
            patch,
            f"git diff --no-index {rel}",
            allowed_returncodes=(0, 1),
        )
        return str(patch.pop("_raw_stdout", patch.get("stdout") or ""))


def run_nexus_fixture(
    fixture_path: Path,
    *,
    out_root: Path,
    nexus_base_url: str,
    nexus_token: str,
    model: str = "coder",
) -> tuple[dict[str, Any], Path]:
    fixture = load_fixture(fixture_path)
    if not nexus_token:
        raise RuntimeError("Nexus bearer token is required (NEXUS_API_KEY or GATEWAY_BEARER_TOKEN)")
    if (
        int(fixture["limits"]["max_agent_steps"]) < NEXUS_MIN_AGENT_STEPS
        or int(fixture["limits"]["wall_time_sec"]) < NEXUS_MIN_WALL_TIME_SEC
    ):
        raise ValueError(
            "Nexus Coding Workspace fixtures require max_agent_steps >= "
            f"{NEXUS_MIN_AGENT_STEPS} and wall_time_sec >= {NEXUS_MIN_WALL_TIME_SEC}"
        )
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    out_root_resolved = out_root.resolve()
    out_root_resolved.mkdir(parents=True, exist_ok=True)
    run_dir = out_root_resolved / run_id
    fixture_dir = run_dir / fixture["id"]
    root = fixture_dir / "nexus"
    artifacts = root / "artifacts"
    task_id = ""
    result: dict[str, Any] | None = None
    result_path = artifacts / "result.json"
    cleanup_ok = False
    failure: BaseException | None = None
    try:
        for directory in (run_dir, fixture_dir, root, artifacts):
            _mkdir_private(directory)
        payload = nexus_api_request(
            "POST",
            nexus_base_url,
            "/coding/harness/runs",
            token=nexus_token,
            body={
                "fixture_id": fixture["id"],
                "files": fixture["repository"]["files"],
                "prompt": fixture["mission"],
                "coding_model": model,
                "commit_message": f"Complete coding harness fixture {fixture['id']}",
                "max_cycles": int(fixture["limits"]["max_agent_steps"]),
                "max_runtime_sec": int(fixture["limits"]["wall_time_sec"]),
            },
            timeout_sec=min(
                60.0,
                max(5.0, float(fixture["limits"]["wall_time_sec"])),
            ),
        )
        task = _task_from_payload(payload)
        task_id = str(task.get("id") or "")
        if not task_id:
            raise NexusApiError("Nexus Coding API returned a task without an id")
        if str(task.get("status") or "") == "error":
            raise NexusApiError(str(task.get("error") or "Nexus harness workspace creation failed"))
        started_at, started, deadline = _nexus_agent_run_clock(
            task,
            wall_time_sec=float(fixture["limits"]["wall_time_sec"]),
        )
        task, run_timed_out = _wait_for_nexus_task(
            task_id,
            base_url=nexus_base_url,
            token=nexus_token,
            deadline=deadline,
        )
        worker_wait_timeout = max(
            NEXUS_GUARDED_WORKER_TIMEOUT_SEC,
            float(fixture["limits"]["wall_time_sec"]),
        )
        diff_payload, evidence_started = _nexus_harness_diff_after_workers(
            task_id,
            base_url=nexus_base_url,
            token=nexus_token,
            wait_timeout_sec=worker_wait_timeout,
        )
        evidence_deadline = evidence_started + MAX_SNAPSHOT_SECONDS
        if diff_payload.get("ok") is not True:
            raise NexusApiError(str(diff_payload.get("error") or "Nexus harness diff collection failed"))
        changes_payload = nexus_api_request(
            "GET",
            nexus_base_url,
            f"/coding/harness/tasks/{quote(task_id, safe='')}/changes",
            token=nexus_token,
            timeout_sec=min(30.0, _snapshot_timeout(evidence_deadline)),
        )
        pending_changes = changes_payload.get("result")
        if not isinstance(pending_changes, dict) or pending_changes.get("ok") is not True:
            detail = pending_changes.get("error") if isinstance(pending_changes, dict) else ""
            raise NexusApiError(
                str(detail or "Nexus harness worktree change collection failed")
            )
        diff_changes = diff_payload.get("changes")
        if not isinstance(diff_changes, dict):
            raise NexusApiError("Nexus harness diff response omitted change evidence")
        if diff_changes.get("truncated") or pending_changes.get("truncated"):
            raise NexusApiError("Nexus harness changed-file evidence was truncated")
        change_items = [
            item for item in (diff_changes.get("files") or [])
            if isinstance(item, dict)
        ]
        change_items.extend(
            item
            for item in (pending_changes.get("files") or [])
            if isinstance(item, dict)
        )
        raw_changed = sorted({
            str(item.get("path") or "")
            for item in change_items
            if str(item.get("path") or "")
        })
        if len(raw_changed) > MAX_SNAPSHOT_CHANGED_FILES:
            raise RuntimeError(f"workspace snapshot exceeds {MAX_SNAPSHOT_CHANGED_FILES} changed-file limit")
        raw_untracked = {
            str(item.get("path") or "")
            for item in (pending_changes.get("files") or [])
            if isinstance(item, dict)
            and (
                str(item.get("kind") or "").strip().lower() == "untracked"
                or str(item.get("status") or "").strip() == "??"
            )
            and str(item.get("path") or "")
        }
        protected = {
            rel for rel in raw_changed
            if _contains_secret_path(rel, [nexus_token])
        }
        safe_changed = sorted(set(raw_changed) - protected)
        safe_untracked = sorted(raw_untracked - protected)
        changed = safe_changed + (["(redacted)"] if protected else [])
        diff_result = diff_payload.get("diff")
        if not isinstance(diff_result, dict):
            raise NexusApiError("Nexus harness diff response omitted diff evidence")
        if diff_result.get("stdout_truncated"):
            raise NexusApiError("Nexus harness diff evidence was truncated")
        diff_text = str(diff_result.get("stdout") or "")

        cache: dict[str, tuple[str | None, str | None]] = {}
        non_text_paths: set[str] = set()
        file_modes: dict[str, str] = {}
        read_content = _nexus_final_file_reader(
            task_id,
            base_url=nexus_base_url,
            token=nexus_token,
            cache=cache,
            non_text_paths=non_text_paths,
            file_modes=file_modes,
            deadline=evidence_deadline,
        )
        expected_paths = {
            str(spec.get("path") or "")
            for key in ("file_contains", "file_not_contains")
            for spec in (fixture.get("expected", {}).get(key) or [])
            if isinstance(spec, dict) and str(spec.get("path") or "")
        }
        for rel in sorted(set(safe_changed) | expected_paths):
            read_content(rel)

        _snapshot_timeout(evidence_deadline)
        if protected:
            diff_text = ""
        else:
            for rel in safe_untracked:
                content, error = cache.get(rel, (None, "file was not fetched"))
                if rel in non_text_paths:
                    continue
                if error or content is None:
                    raise RuntimeError(
                        f"could not collect complete untracked diff evidence for {rel}: "
                        f"{error or 'file unavailable'}"
                    )
                if diff_text and not diff_text.endswith("\n"):
                    diff_text += "\n"
                diff_text += _untracked_text_diff(
                    rel,
                    content,
                    file_modes[rel],
                    deadline=evidence_deadline,
                )
        diff_text = redact_text(diff_text, [nexus_token])
        if len(diff_text) > MAX_SNAPSHOT_DIFF_CHARS:
            raise RuntimeError(f"workspace snapshot exceeds {MAX_SNAPSHOT_DIFF_CHARS} character diff limit")
        (artifacts / "final.diff").write_text(diff_text, encoding="utf-8")

        final_files = artifacts / "final-files"
        final_file_omissions: list[dict[str, str]] = []
        for rel in safe_changed:
            _snapshot_timeout(evidence_deadline)
            content, error = cache.get(rel, (None, "file was not fetched"))
            if error or content is None:
                final_file_omissions.append({"path": rel, "reason": error or "file unavailable"})
                continue
            target = final_files / safe_rel_path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(redact_text(content, [nexus_token]), encoding="utf-8")

        _snapshot_timeout(evidence_deadline)
        deadline += time.monotonic() - evidence_started
        validation = run_nexus_validation(
            fixture,
            task_id,
            base_url=nexus_base_url,
            token=nexus_token,
            deadline=deadline,
        )
        objective = _objective_checks_with_reader(fixture, changed, validation, read_content)
        agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
        run_history = [item for item in (task.get("agent_runs") or []) if isinstance(item, dict)]
        current_run_id = str(agent.get("run_id") or "")
        run_record = next(
            (item for item in reversed(run_history) if str(item.get("run_id") or "") == current_run_id),
            run_history[-1] if run_history else {},
        )
        agent_status = _nexus_agent_status(task)
        completed = bool(agent_status == "completed" and objective.get("passed") is True)
        interrupted = bool(
            run_timed_out
            or validation.get("timed_out")
            or agent_status in {"paused", "stopped", "interrupted", "idle_waiting"}
        )
        if completed:
            outcome_status = "completed"
            outcome_error = None
        elif run_timed_out or validation.get("timed_out"):
            outcome_status = "timed_out"
            outcome_error = "coding run or validation exceeded the fixture wall-time budget"
        elif interrupted:
            outcome_status = agent_status or "interrupted"
            outcome_error = str(agent.get("error") or agent.get("summary") or "coding run interrupted")
        elif validation.get("passed") is False:
            outcome_status = "failed"
            outcome_error = "validation failed"
        elif objective.get("passed") is not True:
            outcome_status = "failed"
            outcome_error = "objective checks failed"
        else:
            outcome_status = "failed"
            outcome_error = str(agent.get("error") or agent.get("summary") or "coding run failed")

        trajectory = _nexus_trajectory(task)
        workspace_info = {
            "base_head": str(diff_payload.get("merge_base") or diff_payload.get("compare_ref") or ""),
            "final_head": str(run_record.get("commit") or (task.get("terminal_result") or {}).get("final_commit") or ""),
            "dirty": bool(changed),
            "files_changed": changed,
            "diff_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
            "diff_chars": len(diff_text),
            "evidence_omissions": (
                ([{"path": "(redacted)", "reason": "path contains a protected secret encoding"}]
                 if protected else [])
                + [
                    {
                        "path": rel,
                        "reason": f"{cache[rel][1]}; untracked patch omitted",
                    }
                    for rel in safe_untracked
                    if rel in non_text_paths
                ]
            ),
            "final_file_omissions": final_file_omissions,
            "git_metadata_retained": True,
            "execution_workspace_retained": True,
        }
        result = redact_value({
            "schema_version": RESULT_SCHEMA_VERSION,
            "fixture_id": fixture["id"],
            "fixture_description": fixture.get("description", ""),
            "tags": fixture.get("tags", []),
            "harness": "nexus-coding-workspace",
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": now_iso(),
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
            "harness_version": {
                "task_schema": "nexus_coding_task.v1",
                "result_source": "v1-coding-api",
            },
            "model": {
                "requested": model,
                "gateway": "nexus",
                "nexus_base_url": nexus_base_url,
                "client_backend": "nexus-coding-workspace",
                "backend": str(run_record.get("backend") or agent.get("backend") or ""),
                "upstream_model": str(run_record.get("upstream_model") or agent.get("upstream_model") or ""),
                "route_evidence": "coding_workspace_persisted_run_record",
                "route_history": trajectory.pop("route_history"),
            },
            "outcome": {
                "status": outcome_status,
                "completed": completed,
                "interrupted": interrupted,
                "exit_code": 0 if completed else None,
                "error": outcome_error,
                "stop_reason_code": str(run_record.get("stop_reason_code") or ""),
            },
            "workspace": workspace_info,
            "validation": validation,
            "trajectory": trajectory,
            "objective": objective,
            "artifacts": {
                "run_root": str(root),
                "stdout": "",
                "stderr": "",
                "diff": str(artifacts / "final.diff"),
                "process_output_truncated": False,
                "trace_files": [],
                "trace_omissions": ["Coding Workspace exposes a bounded public event window, not a raw harness trace."],
                "omitted_non_text": [],
            },
        }, [nexus_token])
        result = _redact_fragmented_value(result, [nexus_token])
        result = _redact_fragmented_value(
            result,
            [nexus_token],
            fields=RESULT_CHILD_CONTROLLED_FIELDS,
            embedded_raw_only=True,
            min_fragment_bytes=MIN_CROSS_FIELD_FRAGMENT_BYTES,
        )
        omitted = _redact_omitted_artifact_paths(
            scrub_retained_artifacts(artifacts, [nexus_token]),
            [nexus_token],
        )
        result["artifacts"]["omitted_non_text"] = omitted
        refresh_diff_metadata(result["workspace"], artifacts / "final.diff")
        result = _redact_fragmented_value(result, [nexus_token])
        result = _redact_fragmented_value(
            result,
            [nexus_token],
            fields=RESULT_CHILD_CONTROLLED_FIELDS,
            embedded_raw_only=True,
            min_fragment_bytes=MIN_CROSS_FIELD_FRAGMENT_BYTES,
        )
    except BaseException as exc:
        failure = exc
    finally:
        if task_id:
            try:
                _delete_nexus_harness_task(
                    task_id,
                    base_url=nexus_base_url,
                    token=nexus_token,
                    wait_timeout_sec=max(
                        NEXUS_GUARDED_WORKER_TIMEOUT_SEC,
                        float(fixture["limits"]["wall_time_sec"]),
                    ),
                )
                cleanup_ok = True
            except BaseException as cleanup_exc:
                if _path_lexists(run_dir):
                    discard_run_root_verified(run_dir)
                raise RuntimeError(
                    "SECURITY: Nexus coding harness workspace could not be deleted: "
                    + redact_text(str(cleanup_exc), [nexus_token])
                ) from cleanup_exc

    if failure is not None:
        if _path_lexists(run_dir):
            try:
                discard_run_root_verified(run_dir)
            except BaseException as discard_exc:
                raise RuntimeError(
                    f"SECURITY: evaluation failed and run directory could not be securely discarded: {run_dir}: "
                    + redact_text(str(discard_exc), [nexus_token])
                ) from discard_exc
        raise failure
    if result is None:
        raise RuntimeError("Nexus coding harness did not produce a result")
    if not cleanup_ok:
        raise RuntimeError("Nexus coding harness cleanup was not confirmed")
    result["workspace"]["git_metadata_retained"] = False
    result["workspace"]["execution_workspace_retained"] = False
    write_json(result_path, result)
    return result, result_path


def run_albatross_fixture(fixture_path: Path, *, out_root: Path, executable: str,
                          nexus_base_url: str, nexus_token: str, model: str = "coder",
                          allow_mutations: bool = True) -> tuple[dict[str, Any], Path]:
    fixture, version = load_fixture(fixture_path), albatross_version(executable)
    if not version["installed"]:
        raise RuntimeError("albatross unavailable; install it separately before running this harness")
    help_result, _, missing_capabilities = albatross_capabilities(version["executable"])
    if not help_result["ok"]:
        detail = help_result["stderr"] or help_result["stdout"] or "unknown --help failure"
        raise RuntimeError(f"could not inspect albatross capabilities: {detail}")
    if missing_capabilities:
        raise RuntimeError(
            "incompatible albatross; missing required capabilities: " + ", ".join(missing_capabilities)
        )
    if not nexus_token:
        raise RuntimeError("Nexus bearer token is required (NEXUS_API_KEY or GATEWAY_BEARER_TOKEN)")
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    out_root_resolved = out_root.resolve()
    out_root_resolved.mkdir(parents=True, exist_ok=True)
    run_dir = out_root_resolved / run_id
    fixture_dir = run_dir / fixture["id"]
    root = fixture_dir / "albatross"
    workspace, artifacts, home, tmp = root / "workspace", root / "artifacts", root / "home", root / "tmp"
    try:
        _mkdir_private(run_dir)
        _mkdir_private(fixture_dir)
        _mkdir_private(root)
        _mkdir_private(home)
        _mkdir_private(tmp)
        baseline = initialize_workspace(workspace, fixture)
        trace_root = home / ".config" / "albatross" / "sessions"
        _write_albatross_runtime_config(workspace, trace_root)
        env = build_albatross_env(nexus_base_url=nexus_base_url, nexus_token=nexus_token, model=model,
                                  workspace=workspace, home=home, temp_dir=tmp,
                                  max_steps=fixture["limits"]["max_agent_steps"], allow_mutations=allow_mutations)
        argv = [version["executable"], "--print", mission_prompt(fixture, allow_mutations)]
        argv.append("--allow-tools")
        started_at, started = now_iso(), time.monotonic()
        deadline = started + float(fixture["limits"]["wall_time_sec"])
        process_timeout = max(0.001, deadline - time.monotonic())
        process = run_process(argv, cwd=workspace, env=env, timeout_sec=process_timeout,
                              secrets=[nexus_token], isolate_process_group=True)
        _require_expected_directory_entries(run_dir, {fixture_dir.name})
        _require_expected_directory_entries(fixture_dir, {root.name})
        _require_expected_directory_entries(
            root,
            {workspace.name, home.name, tmp.name},
            allowed={workspace.name, home.name, tmp.name, artifacts.name},
        )
        _prepare_artifacts_root(root, artifacts)
        trace_started = time.monotonic()
        trace_deadline = time.monotonic() + MAX_TRACE_PARSE_SECONDS
        trace = parse_trace(
            trace_root,
            artifact_dir=artifacts / "traces",
            secrets=[nexus_token],
            deadline=trace_deadline,
            max_agent_steps=fixture["limits"]["max_agent_steps"],
        )
        if trace["agent_turns"] == 0:
            fallback_trace = parse_session_transcripts(
                trace_root,
                artifact_dir=artifacts / "traces",
                secrets=[nexus_token],
                deadline=trace_deadline,
                max_agent_steps=fixture["limits"]["max_agent_steps"],
                max_files=max(0, MAX_TRACE_FILES - len(trace["trace_files"])),
                max_total_bytes=max(
                    0, MAX_TRACE_TOTAL_BYTES - trace["trace_input_bytes"]
                ),
            )
            fallback_trace["trace_files"] = (
                trace["trace_files"] + fallback_trace["trace_files"]
            )
            fallback_trace["trace_omissions"] = (
                trace["trace_omissions"] + fallback_trace["trace_omissions"]
            )
            fallback_trace["malformed_trace_lines"] += trace[
                "malformed_trace_lines"
            ]
            fallback_trace["trace_input_bytes"] += trace["trace_input_bytes"]
            trace = fallback_trace
        deadline += time.monotonic() - trace_started
        validation = run_validation(
            fixture, workspace, home, tmp, deadline=deadline, secrets=[nexus_token]
        )
        _require_expected_directory_entries(run_dir, {fixture_dir.name})
        _require_expected_directory_entries(fixture_dir, {root.name})
        _require_expected_directory_entries(
            root,
            {workspace.name, home.name, tmp.name, artifacts.name},
        )
        (artifacts / "stdout.txt").write_text(process["stdout"], encoding="utf-8")
        (artifacts / "stderr.txt").write_text(process["stderr"], encoding="utf-8")
        workspace_info = workspace_snapshot(workspace, baseline, artifacts, secrets=[nexus_token])
        objective = objective_checks(fixture, workspace, workspace_info["files_changed"], validation)
        omitted_artifacts = scrub_retained_artifacts(artifacts, [nexus_token])
        refresh_diff_metadata(workspace_info, artifacts / "final.diff")

        for execution_path in (workspace, home, tmp):
            discard_path_verified(execution_path)
        if any(_path_lexists(path) for path in (workspace, home, tmp)):
            raise RuntimeError("execution state remained after verified discard")
        _require_expected_directory_entries(root, {artifacts.name})
        _require_expected_directory_entries(fixture_dir, {root.name})
        _require_expected_directory_entries(run_dir, {fixture_dir.name})
        workspace_info["git_metadata_retained"] = False
        workspace_info["execution_workspace_retained"] = False

        duration_ms, finished_at = round((time.monotonic() - started) * 1000.0, 1), now_iso()
        run_timed_out = bool(process["timed_out"] or validation["timed_out"])
        completed = bool(process["ok"] and objective["passed"] is True)
        if not process["ok"]:
            outcome_error = process["stderr"] or f"exit {process['returncode']}"
        elif validation["timed_out"]:
            outcome_error = ("validation time budget exhausted" if validation["budget_exhausted"]
                             else "validation command timed out")
        elif validation.get("passed") is False:
            outcome_error = "validation failed"
        elif objective.get("passed") is not True:
            outcome_error = "objective checks failed"
        else:
            outcome_error = None
        result = redact_value({
            "schema_version": RESULT_SCHEMA_VERSION, "fixture_id": fixture["id"],
            "fixture_description": fixture.get("description", ""), "tags": fixture.get("tags", []),
            "harness": "albatross", "run_id": run_id, "started_at": started_at,
            "finished_at": finished_at, "duration_ms": duration_ms,
            "harness_version": {"version": version["version"], "tested_version": ALBATROSS_TESTED_VERSION,
                                "tested_commit": ALBATROSS_TESTED_COMMIT},
            "model": {"requested": model, "gateway": "nexus", "nexus_base_url": nexus_base_url,
                      "client_backend": "openai", "backend": "", "upstream_model": "",
                      "route_evidence": "not_available_from_albatross_adapter_v1"},
            "outcome": {"status": "completed" if completed else ("timed_out" if run_timed_out else "failed"),
                        "completed": completed, "interrupted": run_timed_out,
                        "exit_code": process["returncode"], "error": outcome_error},
            "workspace": workspace_info, "validation": validation,
            "trajectory": {"agent_turns": trace["agent_turns"], "agent_steps": trace["agent_steps"],
                           "tool_calls": trace["tool_call_count"], "tool_call_names": trace["tool_calls"],
                           "file_read_observed": "file_read" in trace["tool_calls"],
                           "context_resets": trace["context_resets"], "malformed_trace_lines": trace["malformed_trace_lines"],
                           "trace_input_bytes": trace["trace_input_bytes"]},
            "objective": objective,
            "artifacts": {"run_root": str(root), "stdout": str(artifacts / "stdout.txt"),
                          "stderr": str(artifacts / "stderr.txt"), "diff": str(artifacts / "final.diff"),
                          "process_output_truncated": bool(process["output_truncated"]),
                          "trace_files": trace["trace_files"], "trace_omissions": trace["trace_omissions"],
                          "omitted_non_text": omitted_artifacts},
        }, [nexus_token])
        result = _redact_fragmented_value(result, [nexus_token])
        result = _redact_fragmented_value(
            result,
            [nexus_token],
            fields=RESULT_CHILD_CONTROLLED_FIELDS,
            embedded_raw_only=True,
            min_fragment_bytes=MIN_CROSS_FIELD_FRAGMENT_BYTES,
        )
        result_path = artifacts / "result.json"
        write_json(result_path, result)
        return result, result_path
    except BaseException:
        if _path_lexists(run_dir):
            try:
                discard_run_root_verified(run_dir)
            except BaseException as discard_exc:
                discard_detail = redact_text(str(discard_exc), [nexus_token])
                raise RuntimeError(
                    f"SECURITY: evaluation failed and run directory could not be securely discarded: {run_dir}: {discard_detail}"
                ) from discard_exc
        raise


def probe(executable: str, *, live: bool = False, out_root: Path | None = None,
          nexus_base_url: str = "http://ai2:8800/v1", nexus_token: str = "", model: str = "coder") -> dict[str, Any]:
    version = albatross_version(executable)
    report: dict[str, Any] = {"ok": version["installed"],
        "albatross": {**version, "tested_version": ALBATROSS_TESTED_VERSION, "tested_commit": ALBATROSS_TESTED_COMMIT},
        "nexus": {"base_url": nexus_base_url, "model": model}, "capabilities": {}}
    if not version["installed"]:
        return report
    help_result, report["capabilities"], missing = albatross_capabilities(version["executable"])
    report["compatibility"] = {"required": list(REQUIRED_PROBE_CAPABILITIES), "missing": missing}
    report["ok"] = bool(help_result["ok"] and not missing)
    if not live or not report["ok"]:
        return report
    if not nexus_token:
        report["ok"] = False
        report["live_error"] = "Nexus bearer token is required for --live"
        return report
    root = (out_root or Path(".runtime/coding-harness-evals")).resolve()
    probe_dir = root / "probe"
    _ensure_private_directory(probe_dir)
    fixture_path = probe_dir / f"read-only-probe-{uuid.uuid4().hex}.json"
    write_json(fixture_path, {"schema_version": 1, "id": "read-only-probe",
        "description": "Read-only Albatross through Nexus capability probe.",
        "repository": {"files": {"probe.txt": "NEXUS_ALBATROSS_PROBE_OK\n"}},
        "mission": "Use a repository read tool to read probe.txt and report the exact token NEXUS_ALBATROSS_PROBE_OK. Do not edit files.",
        "expected": {"files_changed": [], "file_contains": [{"path": "probe.txt", "needle": "NEXUS_ALBATROSS_PROBE_OK"}]},
        "limits": {"wall_time_sec": 120, "max_agent_steps": 8}, "tags": ["probe", "read-only"]})
    try:
        result, result_path = run_albatross_fixture(fixture_path, out_root=root, executable=executable,
            nexus_base_url=nexus_base_url, nexus_token=nexus_token, model=model, allow_mutations=False)
    except Exception as exc:
        report["ok"] = False
        report["live_error"] = f"{type(exc).__name__}: {exc}"
        return redact_value(report, [nexus_token])
    finally:
        fixture_path.unlink(missing_ok=True)
    response_marker = "NEXUS_ALBATROSS_PROBE_OK"
    response_output = ""
    try:
        response_output = Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8")
    except (KeyError, OSError, TypeError) as exc:
        report["live_error"] = f"could not verify live probe response: {type(exc).__name__}: {exc}"
    response_ok = response_marker in response_output
    chat_ok = result.get("outcome", {}).get("exit_code") == 0 and response_ok
    trajectory = result.get("trajectory", {})
    read_tool_observed = trajectory.get("file_read_observed")
    if read_tool_observed is None:
        read_tool_observed = "file_read" in (trajectory.get("tool_call_names") or [])
    report["capabilities"].update({"chat": chat_ok, "streaming": None,
        "tool_calls": bool(read_tool_observed),
        "structured_trace": bool(trajectory.get("agent_turns"))})
    report["live_result"] = str(result_path)
    report["ok"] = bool(chat_ok and report["capabilities"]["tool_calls"] and result.get("objective", {}).get("passed") is True)
    if not response_ok and "live_error" not in report:
        report["live_error"] = f"live probe response did not contain {response_marker}"
    return redact_value(report, [nexus_token])


def bundled_fixture_paths() -> list[Path]:
    return sorted(Path(__file__).with_name("coding_harness_fixtures").glob("*.json"))


def render_comparison(results: list[dict[str, Any]]) -> str:
    headers = ("harness", "status", "objective", "duration_ms", "steps", "tools", "files", "validation")
    rows = [[str(r.get("harness", "")), str(r.get("outcome", {}).get("status", "")),
             str(r.get("objective", {}).get("passed")), str(r.get("duration_ms", "")),
             str(r.get("trajectory", {}).get("agent_steps", 0)), str(r.get("trajectory", {}).get("tool_calls", 0)),
             str(len(r.get("workspace", {}).get("files_changed") or [])), str(r.get("validation", {}).get("passed"))]
            for r in results]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    lines = ["  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))),
             "  ".join("-" * widths[i] for i in range(len(headers)))]
    return "\n".join(lines + ["  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in rows])


def parse_args() -> argparse.Namespace:
    runtime = Path(os.getenv("NEXUS_RUNTIME_ROOT", ".runtime")) / "coding-harness-evals"
    parser = argparse.ArgumentParser(description="Compare Nexus Coding Workspace and Albatross on common fixtures.")
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list-fixtures")
    ls.add_argument("--json", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("--albatross-bin", default=os.getenv("ALBATROSS_BIN", "albatross"))
    p.add_argument("--base-url", default=os.getenv("NEXUS_BASE_URL", "http://ai2:8800/v1"))
    p.add_argument("--token", default=os.getenv("NEXUS_API_KEY") or os.getenv("GATEWAY_BEARER_TOKEN") or "")
    p.add_argument("--model", default="coder")
    p.add_argument("--out-root", default=str(runtime))
    p.add_argument("--live", action="store_true")
    r = sub.add_parser("run-albatross")
    r.add_argument("--fixture", required=True)
    r.add_argument("--albatross-bin", default=os.getenv("ALBATROSS_BIN", "albatross"))
    r.add_argument("--base-url", default=os.getenv("NEXUS_BASE_URL", "http://ai2:8800/v1"))
    r.add_argument("--token", default=os.getenv("NEXUS_API_KEY") or os.getenv("GATEWAY_BEARER_TOKEN") or "")
    r.add_argument("--model", default="coder")
    r.add_argument("--out-root", default=str(runtime))
    r.add_argument("--read-only", action="store_true")
    n = sub.add_parser("run-nexus")
    n.add_argument("--fixture", required=True)
    n.add_argument("--base-url", default=os.getenv("NEXUS_BASE_URL", "http://ai2:8800/v1"))
    n.add_argument("--token", default=os.getenv("NEXUS_API_KEY") or os.getenv("GATEWAY_BEARER_TOKEN") or "")
    n.add_argument("--model", default="coder")
    n.add_argument("--out-root", default=str(runtime))
    pair = sub.add_parser("run-paired")
    pair.add_argument("--fixture", required=True)
    pair.add_argument("--albatross-bin", default=os.getenv("ALBATROSS_BIN", "albatross"))
    pair.add_argument("--base-url", default=os.getenv("NEXUS_BASE_URL", "http://ai2:8800/v1"))
    pair.add_argument("--token", default=os.getenv("NEXUS_API_KEY") or os.getenv("GATEWAY_BEARER_TOKEN") or "")
    pair.add_argument("--model", default="coder")
    pair.add_argument("--out-root", default=str(runtime))
    pair.add_argument("--json", action="store_true")
    c = sub.add_parser("compare-results")
    c.add_argument("results", nargs="+")
    c.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "list-fixtures":
        values = []
        for path in bundled_fixture_paths():
            fixture = load_fixture(path)
            values.append({"path": str(path), "id": fixture["id"], "description": fixture.get("description", "")})
        print(json.dumps(values, indent=2) if args.json else "\n".join(f"{v['id']}: {v['description']} ({v['path']})" for v in values))
        return 0
    if args.command == "probe":
        report = probe(args.albatross_bin, live=args.live, out_root=Path(args.out_root),
                       nexus_base_url=args.base_url, nexus_token=args.token, model=args.model)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    if args.command == "run-albatross":
        try:
            result, path = run_albatross_fixture(Path(args.fixture), out_root=Path(args.out_root),
                executable=args.albatross_bin, nexus_base_url=args.base_url, nexus_token=args.token,
                model=args.model, allow_mutations=not args.read_only)
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        print(f"result: {path}", file=sys.stderr)
        return 0 if result["outcome"]["completed"] else 1
    if args.command == "run-nexus":
        try:
            result, path = run_nexus_fixture(
                Path(args.fixture),
                out_root=Path(args.out_root),
                nexus_base_url=args.base_url,
                nexus_token=args.token,
                model=args.model,
            )
        except Exception as exc:
            print(f"{type(exc).__name__}: {redact_text(str(exc), [args.token])}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        print(f"result: {path}", file=sys.stderr)
        return 0 if result["outcome"]["completed"] else 1
    if args.command == "run-paired":
        fixture_path = Path(args.fixture)
        try:
            fixture = load_fixture(fixture_path)
            pair_id = f"pair-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
            out_root = Path(args.out_root).resolve()
            out_root.mkdir(parents=True, exist_ok=True)
            pair_root = out_root / pair_id
            _mkdir_private(pair_root)
            nexus_result, nexus_path = run_nexus_fixture(
                fixture_path,
                out_root=pair_root,
                nexus_base_url=args.base_url,
                nexus_token=args.token,
                model=args.model,
            )
            albatross_result, albatross_path = run_albatross_fixture(
                fixture_path,
                out_root=pair_root,
                executable=args.albatross_bin,
                nexus_base_url=args.base_url,
                nexus_token=args.token,
                model=args.model,
            )
            results = [nexus_result, albatross_result]
            manifest = {
                "schema_version": RESULT_SCHEMA_VERSION,
                "pair_id": pair_id,
                "fixture_id": fixture["id"],
                "model": args.model,
                "results": [str(nexus_path), str(albatross_path)],
                "completed": all(item.get("outcome", {}).get("completed") is True for item in results),
                "generated_at": now_iso(),
            }
            manifest_path = pair_root / "comparison.json"
            write_json(manifest_path, manifest)
        except Exception as exc:
            print(f"{type(exc).__name__}: {redact_text(str(exc), [args.token])}", file=sys.stderr)
            return 2
        print(json.dumps({"manifest": manifest, "results": results}, indent=2, sort_keys=True) if args.json else render_comparison(results))
        print(f"comparison: {manifest_path}", file=sys.stderr)
        return 0 if manifest["completed"] else 1
    if args.command == "compare-results":
        results = [read_json(Path(p)) for p in args.results]
        print(json.dumps(results, indent=2, sort_keys=True) if args.json else render_comparison(results))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
