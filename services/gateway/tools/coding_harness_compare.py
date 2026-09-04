#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import hashlib
import json
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
RESERVED_PARTS = {".git", ".albatross", ".small-harness", ".sessions"}
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


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_rel_path(value: str) -> Path:
    raw = "" if value is None else str(value)
    if raw == "":
        raise ValueError(f"unsafe fixture path: {value}")
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe fixture path: {value}")
    if any(part in RESERVED_PARTS for part in path.parts) or path.name in RESERVED_FILES:
        raise ValueError(f"fixture path may not override harness state/config: {value}")
    return path


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
        rel = safe_rel_path(str(raw_path)).as_posix()
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
            expected[key] = [safe_rel_path(str(v)).as_posix() for v in expected[key]]
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
            check["path"] = safe_rel_path(str(check.get("path") or "")).as_posix()
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
                 ["config", "user.name", "Coding Harness Eval"], ["add", "."],
                 ["commit", "-m", "fixture baseline", "--allow-empty"]):
        result = git(list(argv), cwd=workspace)
        if not result["ok"]:
            raise RuntimeError(f"git {' '.join(argv)} failed: {result['stderr'] or result['stdout']}")
    with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write("\n# Nexus harness runtime\n.albatross/\n.small-harness/\n.sessions/\n")
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
            handle.write("* -text -crlf -ident -filter !working-tree-encoding\n")
        attributes.chmod(0o600)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("could not sanitize snapshot Git metadata") from exc


def _is_harness_runtime_path(rel: str) -> bool:
    parts = Path(str(rel or "")).parts
    if not parts:
        return False
    return parts[0] in RESERVED_PARTS


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
    protected_tracked = {rel for rel in tracked_files if _contains_secret_path(rel, secrets)}
    protected_untracked = {rel for rel in raw_untracked_files if _contains_secret_path(rel, secrets)}
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
                        dest_handle.write(json.dumps(redact_value(item, secrets), separators=(",", ":"), sort_keys=True))
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


def objective_checks(fixture: dict[str, Any], workspace: Path, changed: list[str], validation: dict[str, Any]) -> dict[str, Any]:
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
                path, error = _workspace_regular_file(
                    workspace, rel, max_bytes=MAX_OBJECTIVE_FILE_BYTES
                )
                text: str | None = None
                if error == f"file exceeds {MAX_OBJECTIVE_FILE_BYTES} byte limit":
                    error = (
                        f"file exceeds {MAX_OBJECTIVE_FILE_BYTES} byte objective-read limit"
                    )
                if error is None and path is not None:
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        error = f"could not read file: {exc}"
                elif error is None:
                    error = "unsafe file"
                content_cache[rel] = (text, error)
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


def scrub_retained_artifacts(artifacts: Path, secrets: Iterable[str]) -> list[str]:
    secrets = tuple(str(secret) for secret in secrets if secret)
    omitted: list[str] = []
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
        trace = parse_trace(
            home / ".config" / "albatross" / "sessions",
            artifact_dir=artifacts / "traces",
            secrets=[nexus_token],
            deadline=time.monotonic() + MAX_TRACE_PARSE_SECONDS,
            max_agent_steps=fixture["limits"]["max_agent_steps"],
        )
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
                           "context_resets": trace["context_resets"], "malformed_trace_lines": trace["malformed_trace_lines"],
                           "trace_input_bytes": trace["trace_input_bytes"]},
            "objective": objective,
            "artifacts": {"run_root": str(root), "stdout": str(artifacts / "stdout.txt"),
                          "stderr": str(artifacts / "stderr.txt"), "diff": str(artifacts / "final.diff"),
                          "process_output_truncated": bool(process["output_truncated"]),
                          "trace_files": trace["trace_files"], "trace_omissions": trace["trace_omissions"],
                          "omitted_non_text": omitted_artifacts},
        }, [nexus_token])
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
    tools = result.get("trajectory", {}).get("tool_call_names") or []
    response_marker = "NEXUS_ALBATROSS_PROBE_OK"
    response_output = ""
    try:
        response_output = Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8")
    except (KeyError, OSError, TypeError) as exc:
        report["live_error"] = f"could not verify live probe response: {type(exc).__name__}: {exc}"
    response_ok = response_marker in response_output
    chat_ok = result.get("outcome", {}).get("exit_code") == 0 and response_ok
    report["capabilities"].update({"chat": chat_ok, "streaming": None,
        "tool_calls": "file_read" in tools, "structured_trace": bool(result.get("artifacts", {}).get("trace_files"))})
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
    parser = argparse.ArgumentParser(description="Run Albatross as an external coding harness through Nexus.")
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
    if args.command == "compare-results":
        results = [read_json(Path(p)) for p in args.results]
        print(json.dumps(results, indent=2, sort_keys=True) if args.json else render_comparison(results))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
