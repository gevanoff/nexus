from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_routes
from app import coding_agent as ca
from app import coding_process_supervisor as supervisor
from app import coding_workspace as cw


def _linux_process_containment_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text(
            encoding="ascii"
        )
        return True
    except OSError:
        return False


_LINUX_PROCESS_CONTAINMENT_AVAILABLE = _linux_process_containment_available()


def _configure_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")


def test_create_harness_task_materializes_local_baseline(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)

    task = cw.create_harness_task(
        fixture_id="small-fix",
        files={"src/app.py": "VALUE = 'broken'\n", "test_app.py": "pass\n"},
        prompt="Fix VALUE.",
        owner="test",
        coding_model="coder",
    )

    assert task["kind"] == "harness_eval"
    assert task["status"] == "ready"
    assert task["repo_url"] == "harness-fixture://small-fix"
    assert task["branch_name"].startswith("nexus-coding-harness/")
    raw = cw.load_task(task["id"])
    assert raw["harness_fixture"] == {
        "id": "small-fix",
        "file_count": 2,
        "total_bytes": len("VALUE = 'broken'\npass\n"),
    }
    assert raw["harness_baseline_commit"]
    assert raw["harness_expires_at"] > raw["created_at"]
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 'broken'\n"
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip() == task["branch_name"]
    assert subprocess.run(
        ["git", "remote"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout == ""
    assert cw.git_diff(task["id"])["changes"]["counts"]["total"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/absolute.py",
        "nested\\windows.py",
        ".git/config",
        ".GIT/config",
        "a//b.py",
    ],
)
def test_create_harness_task_rejects_unsafe_paths_before_writing(monkeypatch, tmp_path, path):
    _configure_roots(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        cw.create_harness_task(
            fixture_id="unsafe",
            files={path: "content"},
            prompt="Do work.",
            owner="test",
        )

    assert exc_info.value.status_code == 400
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "tasks").exists()


def test_create_harness_task_enforces_aggregate_limit(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "_HARNESS_MAX_TOTAL_BYTES", 3)
    monkeypatch.setattr(cw, "file_max_bytes", lambda: 100)

    with pytest.raises(HTTPException) as exc_info:
        cw.create_harness_task(
            fixture_id="too-large",
            files={"a.txt": "aa", "b.txt": "bb"},
            prompt="Do work.",
            owner="test",
        )

    assert exc_info.value.status_code == 413
    assert "aggregate" in str(exc_info.value.detail)


def test_harness_validation_uses_mission_timeout_without_git_credentials(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="long-validation",
        files={"test_app.py": "pass\n"},
        prompt="Validate the fixture.",
        owner="test",
        mission_overrides=cw.coding_mission_overrides(max_runtime_sec=300),
    )
    captured = {}

    def fake_run_process(argv, **kwargs):
        captured.update({"argv": list(argv), **kwargs})
        cw.mutate_task(
            task["id"],
            lambda current: current.update(validation_observer="newer-state"),
        )
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", fake_run_process)

    result = cw.run_harness_validation_command(
        task["id"],
        argv=["git", "status", "--short"],
        timeout_sec=250,
    )

    assert result["ok"] is True
    assert captured["timeout_sec"] == 250
    assert captured["timeout_limit_sec"] == 300
    assert captured["use_git_credentials"] is False
    assert captured["isolate_process_group"] is True
    assert cw.load_task(task["id"])["validation_observer"] == "newer-state"


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_validation_contains_detached_descendant(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-descendant",
        files={"test_app.py": "pass\n"},
        prompt="Contain validation descendants.",
        owner="test",
    )
    marker = tmp_path / "descendant-wrote"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.5); Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True)"
    )

    result = cw.run_harness_validation_command(
        task["id"],
        argv=["python3", "-c", parent],
        timeout_sec=5,
    )

    assert result["ok"] is True
    time.sleep(0.7)
    assert not marker.exists()


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_task_command_contains_detached_descendant(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="tool-command-descendant",
        files={"app.py": "before\n"},
        prompt="Contain task-command descendants.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    child = (
        "import time; from pathlib import Path; "
        "time.sleep(0.5); Path('late.txt').write_text('escaped', encoding='utf-8')"
    )
    parent = (
        "import subprocess, sys; from pathlib import Path; "
        "Path('immediate.txt').write_text('kept', encoding='utf-8'); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True)"
    )

    result = cw.run_task_command(
        task["id"],
        argv=["python3", "-c", parent],
        timeout_sec=5,
    )

    assert result["ok"] is True, result
    assert repo.joinpath("immediate.txt").read_text(encoding="utf-8") == "kept"
    time.sleep(0.7)
    assert not repo.joinpath("late.txt").exists()


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_task_command_timeout_contains_detached_descendant(
    monkeypatch, tmp_path
):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="tool-command-timeout-descendant",
        files={"app.py": "before\n"},
        prompt="Contain task-command descendants after timeout.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    child = (
        "import time; from pathlib import Path; "
        "time.sleep(1.5); Path('late.txt').write_text('escaped', encoding='utf-8')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True); time.sleep(5)"
    )

    result = cw.run_task_command(
        task["id"],
        argv=["python3", "-c", parent],
        timeout_sec=1,
    )

    assert result["ok"] is False
    assert result["returncode"] is None
    time.sleep(0.7)
    assert not repo.joinpath("late.txt").exists()


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_validation_timeout_contains_detached_descendant(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-timeout-descendant",
        files={"test_app.py": "pass\n"},
        prompt="Contain validation descendants after a timeout.",
        owner="test",
        mission_overrides=cw.coding_mission_overrides(max_runtime_sec=1),
    )
    marker = tmp_path / "timeout-descendant-wrote"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(1.5); Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True); time.sleep(5)"
    )

    result = cw.run_harness_validation_command(
        task["id"],
        argv=["python3", "-c", parent],
        timeout_sec=1,
    )

    assert result["ok"] is False
    assert result["returncode"] is None
    time.sleep(0.7)
    assert not marker.exists()


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_validation_bounds_noisy_output_while_running(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "max_output_chars", lambda: 2_000)
    task = cw.create_harness_task(
        fixture_id="validation-noisy-output",
        files={"test_app.py": "pass\n"},
        prompt="Bound noisy validation output.",
        owner="test",
    )

    result = cw.run_harness_validation_command(
        task["id"],
        argv=[
            "python3",
            "-c",
            "import sys; print('o' * 2000000); print('e' * 2000000, file=sys.stderr)",
        ],
        timeout_sec=5,
    )

    assert result["ok"] is True
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert len(result["stdout"]) < 3_000
    assert len(result["stderr"]) < 3_000


def test_process_supervisor_fails_closed_without_procfs(monkeypatch):
    def unavailable(_path):
        raise FileNotFoundError("procfs unavailable")

    monkeypatch.setattr(supervisor.os, "scandir", unavailable)

    with pytest.raises(RuntimeError, match="procfs child enumeration is unavailable"):
        supervisor._direct_children(os.getpid(), required=True)


def test_process_supervisor_applies_child_resource_limits(monkeypatch):
    limits = []
    identity_calls = []
    cgroup_calls = []
    seccomp_calls = []
    cgroup = Path("/sys/fs/cgroup/nexus-validation-test")
    monkeypatch.setattr(
        supervisor,
        "_join_validation_cgroup",
        lambda value: cgroup_calls.append(value),
    )
    monkeypatch.setattr(
        supervisor.resource,
        "setrlimit",
        lambda kind, value: limits.append((kind, value)),
    )
    monkeypatch.setattr(
        supervisor.resource,
        "getrlimit",
        lambda _kind: (
            supervisor.resource.RLIM_INFINITY,
            supervisor.resource.RLIM_INFINITY,
        ),
    )
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        supervisor,
        "_install_network_seccomp",
        lambda: seccomp_calls.append(True),
    )
    monkeypatch.setattr(
        supervisor.os,
        "setgroups",
        lambda value: identity_calls.append(("groups", value)),
    )
    monkeypatch.setattr(
        supervisor.os,
        "setgid",
        lambda value: identity_calls.append(("gid", value)),
    )
    monkeypatch.setattr(
        supervisor.os,
        "setuid",
        lambda value: identity_calls.append(("uid", value)),
    )

    supervisor._apply_child_limits(
        uid=65_534,
        gid=65_534,
        file_bytes=1_024,
        open_files=64,
        processes=128,
        memory_bytes=2_048,
        cgroup=cgroup,
    )

    assert limits == [
        (supervisor.resource.RLIMIT_FSIZE, (1_024, 1_024)),
        (supervisor.resource.RLIMIT_NOFILE, (64, 64)),
        (supervisor.resource.RLIMIT_NPROC, (128, 128)),
        (supervisor.resource.RLIMIT_AS, (2_048, 2_048)),
    ]
    assert cgroup_calls == [cgroup]
    assert identity_calls == [("groups", []), ("gid", 65_534), ("uid", 65_534)]
    assert seccomp_calls == [True]


def test_process_supervisor_keeps_scratch_root_outside_child_ownership(
    monkeypatch, tmp_path
):
    root = tmp_path / "root"
    workspace = root / "workspace"
    scratch = root / "scratch"
    workspace.mkdir(parents=True)
    scratch.mkdir()
    nested = workspace / "shared"
    nested.mkdir()
    workspace.chmod(0o2751)
    nested.chmod(0o1777)
    test_app = workspace / "test_app.py"
    test_app.write_text("pass\n", encoding="utf-8")
    test_app.chmod(0o6754)
    chowns = []

    def chown_and_clear_setgid(path, uid, gid, **kwargs):
        path = Path(path)
        chowns.append((path, uid, gid))
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & 0o777)

    monkeypatch.setattr(
        supervisor.os,
        "chown",
        chown_and_clear_setgid,
    )
    effective_groups = []
    monkeypatch.setattr(supervisor.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        supervisor.os,
        "setegid",
        lambda gid: effective_groups.append(gid),
    )

    supervisor._prepare_validation_tree(root, uid=65_534, gid=65_534)

    assert root not in {item[0] for item in chowns}
    assert workspace in {item[0] for item in chowns}
    assert scratch in {item[0] for item in chowns}
    assert (root.stat().st_mode & 0o777) == 0o711
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o2751
    assert stat.S_IMODE(nested.stat().st_mode) == 0o1777
    assert stat.S_IMODE(test_app.stat().st_mode) == 0o6754
    assert effective_groups == [65_534, 0]


def test_process_supervisor_configures_kernel_memory_and_process_limits(
    monkeypatch, tmp_path
):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.subtree_control").write_text("memory pids\n", encoding="ascii")
    writes = []
    real_mkdir = Path.mkdir

    def cgroup_mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", cgroup_mkdir)
    monkeypatch.setattr(
        supervisor,
        "_write_cgroup_value",
        lambda path, value: writes.append((Path(path).name, value)),
    )
    monkeypatch.setattr(supervisor.secrets, "token_hex", lambda _size: "fixed")

    group = supervisor._create_validation_cgroup(
        root,
        memory_bytes=2_048,
        processes=16,
    )

    assert group == root / f"nexus-validation-{os.getpid()}-fixed"
    assert writes == [
        ("memory.max", "2048"),
        ("memory.swap.max", "0"),
        ("memory.oom.group", "1"),
        ("pids.max", "16"),
    ]


def test_process_supervisor_landlock_only_allows_staged_tree_and_system_reads():
    root = Path("/tmp/nexus-validation-test")

    argv = supervisor._landlocked_argv(["python3", "-m", "unittest"], writable_root=root)

    assert argv[-4:] == ["--", "python3", "-m", "unittest"]
    assert f"fs:{supervisor._LANDLOCK_ACCESS}" in argv
    assert f"path-beneath:{supervisor._LANDLOCK_ACCESS}:{root}" in argv
    assert f"path-beneath:{supervisor._LANDLOCK_READ_ACCESS}:/usr" in argv
    assert not any("/var/lib/gateway" in item for item in argv)
    assert "path-beneath:write-file:/dev/null" in argv


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux seccomp containment required",
)
def test_validation_process_denies_network_syscalls(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    script = (
        "import errno, socket\n"
        "for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), "
        "(socket.AF_INET, socket.SOCK_DGRAM), (socket.AF_UNIX, socket.SOCK_STREAM)):\n"
        "    try:\n"
        "        socket.socket(family, kind)\n"
        "    except PermissionError as exc:\n"
        "        assert exc.errno == errno.EPERM\n"
        "    else:\n"
        "        raise AssertionError('network socket was available')\n"
        "print('network-denied')\n"
    )

    result = cw._run_contained_process(
        ["python3", "-c", script],
        cwd=source,
        env=dict(os.environ),
        timeout_sec=10,
        decode_errors="replace",
        output_limit_chars=10_000,
    )

    assert result[0] == 0, result
    assert result[1] == "network-denied\n"
    assert result[4] == ""


def test_harness_agent_tool_surface_omits_command_execution():
    task = {"id": "code_abcdef123456", "kind": "harness_eval"}
    names = {
        spec.function.name
        for spec in ca._filter_harness_tool_specs(ca._tool_specs(), task)
    }

    assert "coding_run_command" not in names
    assert "coding_read_file" in names
    assert "coding_replace_text" in names
    assert ca._harness_agent_tool_blocked(task, "coding_run_command") is True
    assert ca._harness_agent_tool_blocked(task, "coding_read_file") is False


def test_harness_finish_gate_defers_validation_but_still_requires_diff_review():
    task = {"id": "code_abcdef123456", "kind": "harness_eval"}
    gate_args = {
        "task": task,
        "finish_success": True,
        "workspace_modified": True,
        "validation_run_after_edit": False,
        "validation_ok_after_edit": None,
    }

    assert ca._finish_gate_feedback(
        **gate_args,
        diff_reviewed_after_edit=True,
    ) == ""
    assert "coding_git_diff" in ca._finish_gate_feedback(
        **gate_args,
        diff_reviewed_after_edit=False,
    )
    assert ca._finish_gate_required_tools(task) == ["coding_git_diff"]

    normal_task = {"id": "code_abcdef123456", "kind": "workspace"}
    normal_feedback = ca._finish_gate_feedback(
        **{**gate_args, "task": normal_task},
        diff_reviewed_after_edit=True,
    )
    assert "validation command" in normal_feedback
    assert ca._finish_gate_required_tools(normal_task) == [
        "coding_run_command",
        "coding_git_diff",
    ]


def test_validation_workspace_staging_preserves_symlink_without_dereference(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source.joinpath("escape").symlink_to(tmp_path)

    stage_bytes, stage_entries = cw._stage_validation_workspace(source, destination)

    assert destination.joinpath("escape").is_symlink()
    assert os.readlink(destination / "escape") == str(tmp_path)
    assert stage_bytes == len(os.fsencode(str(tmp_path)))
    assert stage_entries == 1
    assert supervisor._scratch_usage(destination) == (stage_bytes, stage_entries)


def test_validation_workspace_staging_retargets_absolute_internal_symlink(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source.joinpath("target.txt").write_text("before\n", encoding="utf-8")
    source.joinpath("alias.txt").symlink_to(source / "target.txt")

    stage_bytes, stage_entries = cw._stage_validation_workspace(source, destination)

    staged_target = destination / "target.txt"
    staged_alias = destination / "alias.txt"
    assert staged_alias.is_symlink()
    assert os.readlink(staged_alias) == str(staged_target)
    staged_alias.write_text("after\n", encoding="utf-8")
    assert staged_target.read_text(encoding="utf-8") == "after\n"
    assert source.joinpath("target.txt").read_text(encoding="utf-8") == "before\n"
    assert stage_bytes == len(b"before\n") + len(os.fsencode(str(staged_target)))
    assert stage_entries == 2


def test_validation_workspace_staging_preserves_dotdot_symlink_resolution(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source.joinpath("target.txt").write_text("value\n", encoding="utf-8")
    raw_target = f"{source}/missing/../target.txt"
    source.joinpath("alias.txt").symlink_to(raw_target)

    stage_bytes, stage_entries = cw._stage_validation_workspace(source, destination)

    staged_alias = destination / "alias.txt"
    staged_raw_target = f"{destination}/missing/../target.txt"
    assert staged_alias.is_symlink()
    assert os.readlink(staged_alias) == staged_raw_target
    assert not staged_alias.exists()
    assert stage_bytes == len(b"value\n") + len(os.fsencode(staged_raw_target))
    assert stage_entries == 2
    assert supervisor._scratch_usage(destination) == (stage_bytes, stage_entries)


def test_validation_workspace_staging_preserves_hard_link_identity(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source.joinpath("first.txt").write_text("before\n", encoding="utf-8")
    os.link(source / "first.txt", source / "second.txt")

    stage_bytes, stage_entries = cw._stage_validation_workspace(source, destination)

    staged_first = destination / "first.txt"
    staged_second = destination / "second.txt"
    assert staged_first.stat().st_ino == staged_second.stat().st_ino
    assert stage_bytes == 2 * len(b"before\n")
    assert stage_entries == 2
    assert supervisor._scratch_usage(destination) == (stage_bytes, stage_entries)
    staged_first.write_text("after\n", encoding="utf-8")
    assert staged_second.read_text(encoding="utf-8") == "after\n"
    assert source.joinpath("first.txt").read_text(encoding="utf-8") == "before\n"
    assert source.joinpath("second.txt").read_text(encoding="utf-8") == "before\n"


def test_validation_workspace_staging_preserves_directory_modes(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir(mode=0o751)
    locked = source / "locked"
    locked.mkdir()
    locked.joinpath("value.txt").write_text("value\n", encoding="utf-8")
    source.chmod(0o2751)
    locked.chmod(0o1555)

    cw._stage_validation_workspace(source, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o2751
    assert stat.S_IMODE(destination.joinpath("locked").stat().st_mode) == 0o1555
    destination.joinpath("locked").chmod(0o700)
    destination.chmod(0o700)
    locked.chmod(0o700)


def test_validation_workspace_staging_preserves_regular_file_modes(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    executable = source / "executable"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o6754)

    cw._stage_validation_workspace(source, destination)

    assert stat.S_IMODE(destination.joinpath("executable").stat().st_mode) == 0o6754


def test_validation_workspace_staging_enforces_streamed_byte_limit(
    monkeypatch, tmp_path
):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source.joinpath("large.bin").write_bytes(b"x" * 2_048)
    monkeypatch.setattr(cw, "_HARNESS_VALIDATION_STAGE_BYTES", 1_024)

    with pytest.raises(RuntimeError, match="staging byte limit"):
        cw._stage_validation_workspace(source, destination)


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_validation_uses_writable_disposable_workspace(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-staged-workspace",
        files={"test_app.py": "pass\n"},
        prompt="Use a staged validation workspace.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"

    result = cw.run_harness_validation_command(
        task["id"],
        argv=[
            "python3",
            "-c",
            "from pathlib import Path; Path('generated.txt').write_text('ok')",
        ],
        timeout_sec=5,
    )

    assert result["ok"] is True
    assert not repo.joinpath("generated.txt").exists()


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_evidence_lease_preserves_validation_mutations(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-evidence-workspace",
        files={"app.py": "before\n"},
        prompt="Preserve validation artifacts for evidence.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)
    lease_id = lease["lease_id"]
    record = cw._ACTIVE_HARNESS_EVIDENCE_LEASES[lease_id]
    evidence_workspace = Path(record["workspace"])

    created = cw.run_harness_validation_command(
        task["id"],
        argv=[
            "python3",
            "-c",
            (
                "from pathlib import Path; Path('build').mkdir(); "
                "Path('build/generated.txt').write_text('artifact\\n')"
            ),
        ],
        evidence_lease_id=lease_id,
    )
    consumed = cw.run_harness_validation_command(
        task["id"],
        argv=[
            "python3",
            "-c",
            (
                "from pathlib import Path; "
                "assert Path('generated.txt').read_text() == 'artifact\\n'; "
                "Path('../app.py').write_text('after\\n')"
            ),
        ],
        cwd="build",
        evidence_lease_id=lease_id,
    )

    assert created["ok"] is True
    assert consumed["ok"] is True, consumed
    assert cw.read_harness_file_evidence(
        task["id"],
        path="build/generated.txt",
        evidence_lease_id=lease_id,
    )["content"] == "artifact\n"
    assert cw.read_harness_file_evidence(
        task["id"],
        path="app.py",
        evidence_lease_id=lease_id,
    )["content"] == "after\n"
    changed = cw.harness_git_changes(
        task["id"],
        evidence_lease_id=lease_id,
    )
    assert {item["path"] for item in changed["files"]} >= {
        "app.py",
        "build/generated.txt",
    }
    assert repo.joinpath("app.py").read_text(encoding="utf-8") == "before\n"
    assert not repo.joinpath("build").exists()

    assert cw.delete_harness_task(
        task["id"],
        evidence_lease_id=lease_id,
    )["ok"] is True
    assert not evidence_workspace.parent.exists()


@pytest.mark.skipif(
    not _LINUX_PROCESS_CONTAINMENT_AVAILABLE,
    reason="Linux procfs containment required",
)
def test_harness_validation_enforces_file_size_limit(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "_HARNESS_VALIDATION_FILE_BYTES", 1_024)
    task = cw.create_harness_task(
        fixture_id="validation-file-limit",
        files={"test_app.py": "pass\n"},
        prompt="Enforce validation file limits.",
        owner="test",
    )

    result = cw.run_harness_validation_command(
        task["id"],
        argv=[
            "python3",
            "-c",
            "import os; from pathlib import Path; "
            "Path(os.environ['TMPDIR'] + '/large').write_bytes(b'x' * 2048)",
        ],
        timeout_sec=5,
    )

    assert result["ok"] is False
    assert result["returncode"] != 0


def test_harness_validation_preserves_argument_whitespace(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-whitespace",
        files={"test_app.py": "pass\n"},
        prompt="Validate exact argument data.",
        owner="test",
    )
    captured = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = list(argv)
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", fake_run_process)

    cw.run_harness_validation_command(
        task["id"],
        argv=["python3", "-c", "import sys; assert sys.argv[1] == '  '", "  "],
    )

    assert captured["argv"][-1] == "  "


def test_harness_validation_rejects_normal_workspace(monkeypatch):
    monkeypatch.setattr(cw, "load_task", lambda task_id: {"id": task_id, "kind": "workspace"})

    with pytest.raises(HTTPException) as exc_info:
        cw.run_harness_validation_command("code_abcdef123456", argv=["python3", "-m", "unittest"])

    assert exc_info.value.status_code == 403


def test_run_process_timeout_override_is_bounded_but_can_exceed_default(monkeypatch, tmp_path):
    monkeypatch.setattr(cw.S, "CODING_COMMAND_TIMEOUT_SEC", 120)
    observed = []

    def fake_run(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cw.subprocess, "run", fake_run)

    cw._run_process(["python3", "--version"], cwd=tmp_path, timeout_sec=250)
    cw._run_process(
        ["python3", "--version"],
        cwd=tmp_path,
        timeout_sec=250,
        timeout_limit_sec=300,
    )

    assert observed == [120, 250]


def test_harness_git_evidence_requests_one_over_changed_file_limit(monkeypatch):
    monkeypatch.setattr(
        cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "harness_eval"},
    )
    captured = {}

    def fake_snapshot(task, *, change_limit, repo_override=None):
        captured.setdefault("calls", []).append(
            (task["id"], change_limit, repo_override)
        )
        return {"diff": {"ok": True}, "changes": {"ok": True}}

    monkeypatch.setattr(cw, "_harness_neutral_git_snapshot", fake_snapshot)

    assert cw.harness_git_diff("code_abcdef123456") == {"ok": True}
    assert cw.harness_git_changes("code_abcdef123456") == {"ok": True}
    assert captured == {"calls": [("code_abcdef123456", 513, None)] * 2}


def test_harness_git_evidence_ignores_repository_git_controls(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="neutral-git-evidence",
        files={".gitattributes": "*.py -diff\n", "app.py": "VALUE = 1\n"},
        prompt="Change the value.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.external", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "app.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    changes = cw.harness_git_changes(task["id"])
    diff = cw.harness_git_diff(task["id"])

    assert changes["ok"] is True
    assert {item["path"] for item in changes["files"]} == {"app.py"}
    assert diff["ok"] is True
    assert {item["path"] for item in diff["changes"]["files"]} == {"app.py"}
    assert "+VALUE = 2" in diff["diff"]["stdout"]


def test_harness_git_diff_uses_harness_output_bound(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "max_output_chars", lambda: 40_000)
    real_run_process = cw._run_process
    observed_limits = {}

    def capturing_run_process(argv, **kwargs):
        if "ls-files" in argv:
            observed_limits["untracked"] = kwargs.get("output_limit_chars")
        if "--name-status" in argv:
            observed_limits["tracked"] = kwargs.get("output_limit_chars")
        return real_run_process(argv, **kwargs)

    monkeypatch.setattr(cw, "_run_process", capturing_run_process)
    task = cw.create_harness_task(
        fixture_id="large-neutral-diff",
        files={"large.txt": "original\n"},
        prompt="Create a large but valid textual diff.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "large.txt").write_text("changed\n" * 10_000, encoding="utf-8")

    result = cw.harness_git_diff(task["id"])

    assert result["ok"] is True
    assert result["diff"]["stdout_truncated"] is False
    assert len(result["diff"]["stdout"]) > 40_000
    assert "worktree_diff" not in result
    assert "worktree_stat" not in result
    assert observed_limits == {
        "untracked": cw._HARNESS_MAX_DIFF_CHARS,
        "tracked": cw._HARNESS_MAX_DIFF_CHARS,
    }


def test_harness_limits_do_not_depend_on_normal_coding_file_cap(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(cw, "file_max_bytes", lambda: 1)

    task = cw.create_harness_task(
        fixture_id="shared-limit",
        files={"value.txt": "valid fixture content"},
        prompt="Inspect the fixture.",
        owner="test",
    )

    evidence = cw.read_harness_file_evidence(task["id"], path="value.txt")
    assert evidence["encoding"] == "utf-8"
    assert evidence["content"] == "valid fixture content"


def test_run_task_command_preserves_newer_terminal_lifecycle_state(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="command-terminal-race",
        files={"app.py": "value\n"},
        prompt="Run a command.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="running"),
    )

    def fake_run_process(argv, **kwargs):
        cw.mutate_task(
            task["id"],
            lambda current: current.update(
                agent_status="paused",
                stop_reason_code="run_timeout",
            ),
        )
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "done\n",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", fake_run_process)

    assert cw.run_task_command(task["id"], argv=["python3", "-V"])["ok"] is True
    saved = cw.load_task(task["id"])
    assert saved["agent_status"] == "paused"
    assert saved["stop_reason_code"] == "run_timeout"
    assert saved["commands"][-1]["label"] == "command"


def test_harness_git_evidence_keeps_pre_index_rename_target_untracked(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="literal-paths",
        files={"café.txt": "old\n", "old name.txt": "rename me\n"},
        prompt="Change files with unusual names.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "café.txt").write_text("new\n", encoding="utf-8")
    (repo / 'quote"file.txt').write_text("added\n", encoding="utf-8")
    subprocess.run(
        ["git", "mv", "old name.txt", "new name.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    changed = cw.harness_git_changes(task["id"])["files"]
    diff = cw.harness_git_diff(task["id"])
    diff_changed = diff["changes"]["files"]

    expected = {"café.txt", 'quote"file.txt', "old name.txt", "new name.txt"}
    assert {item["path"] for item in changed} == expected
    assert {item["path"] for item in diff_changed} == expected
    by_path = {item["path"]: item for item in changed}
    assert by_path["old name.txt"]["kind"] == "removed"
    assert by_path["new name.txt"]["kind"] == "untracked"
    assert by_path["new name.txt"]["status"] == "??"
    diff_text = diff["diff"]["stdout"]
    assert "deleted file mode" in diff_text
    assert "rename from" not in diff_text
    assert "rename to" not in diff_text
    assert 'quote"file.txt' not in diff_text
    assert "new name.txt" not in diff_text


def test_harness_git_evidence_rejects_non_utf8_path_explicitly(monkeypatch, tmp_path):
    if os.name != "posix":
        pytest.skip("non-UTF-8 filename regression requires POSIX surrogateescape")
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="non-utf8-path",
        files={"app.py": "value\n"},
        prompt="Inspect unusual paths.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    raw_path = os.fsencode(repo) + b"/non-utf8-\xff.txt"
    descriptor = os.open(raw_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, b"content\n")
    finally:
        os.close(descriptor)

    changes = cw.harness_git_changes(task["id"])
    diff = cw.harness_git_diff(task["id"])

    assert changes["ok"] is False
    assert changes["files"] == []
    assert changes["error"] == "harness repository contains a path that is not valid UTF-8"
    assert diff["ok"] is False
    assert diff["error"] == changes["error"]
    json.dumps({"changes": changes, "diff": diff}).encode("utf-8")


def test_harness_file_evidence_is_strict_text_or_explicit_binary(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="file-evidence",
        files={"text.txt": "hello\n", " spaced.txt ": "exact\n"},
        prompt="Inspect evidence.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "binary.dat").write_bytes(b"\xff\x00payload")
    (repo / "link.txt").symlink_to("text.txt")

    changes = {
        item["path"]: item for item in cw.harness_git_changes(task["id"])["files"]
    }
    assert changes["binary.dat"]["kind"] == "untracked"
    assert changes["binary.dat"]["status"] == "??"
    assert changes["link.txt"]["kind"] == "untracked"
    assert changes["link.txt"]["status"] == "??"
    diff_text = cw.harness_git_diff(task["id"])["diff"]["stdout"]
    assert "binary.dat" not in diff_text
    assert "link.txt" not in diff_text

    assert cw.read_harness_file_evidence(task["id"], path="text.txt") == {
        "path": "text.txt",
        "size": 6,
        "mode": "100644",
        "encoding": "utf-8",
        "content": "hello\n",
    }
    assert cw.read_harness_file_evidence(task["id"], path="binary.dat") == {
        "path": "binary.dat",
        "size": 9,
        "encoding": "binary",
        "content": None,
    }
    assert cw.read_harness_file_evidence(task["id"], path=" spaced.txt ")["content"] == "exact\n"
    assert cw.read_harness_file_evidence(task["id"], path="link.txt") == {
        "path": "link.txt",
        "size": 8,
        "encoding": "symlink",
        "content": None,
    }


def test_harness_file_evidence_rejects_normal_workspace(monkeypatch):
    monkeypatch.setattr(
        cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "workspace"},
    )

    with pytest.raises(HTTPException) as exc_info:
        cw.read_harness_file_evidence("code_abcdef123456", path="app.py")

    assert exc_info.value.status_code == 403


def test_cleanup_expired_harness_tasks_only_removes_settled_terminal_evals(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    terminal = cw.create_harness_task(
        fixture_id="terminal",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    active = cw.create_harness_task(
        fixture_id="active",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    abandoned = cw.create_harness_task(
        fixture_id="abandoned",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    initialization_error = cw.create_harness_task(
        fixture_id="initialization-error",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    abandoned_initializing = cw.create_harness_task(
        fixture_id="abandoned-initializing",
        files={"app.py": "value\n"},
        prompt="Change value.",
        owner="test",
    )
    finalization_failures = [
        (
            cw.create_harness_task(
                fixture_id=status,
                files={"app.py": "value\n"},
                prompt="Change value.",
                owner="test",
            ),
            status,
        )
        for status in ("failed_finalization", "failed_publish")
    ]
    now = 2_000_000_000.0
    statuses = [
        (terminal["id"], "completed"),
        (active["id"], "running"),
        *((task["id"], status) for task, status in finalization_failures),
    ]
    for task_id, status in statuses:
        raw = cw.load_task(task_id)
        raw["harness_expires_at"] = now - 100
        raw["agent_status"] = status
        raw["agent_finished_at"] = now - 60
        cw.save_task(raw)
    raw_error = cw.load_task(initialization_error["id"])
    raw_error["harness_expires_at"] = now - 100
    raw_error["status"] = "error"
    raw_error["updated_at"] = now - 60
    cw.save_task(raw_error)
    raw_abandoned = cw.load_task(abandoned["id"])
    raw_abandoned["harness_expires_at"] = now - 100
    raw_abandoned["updated_at"] = now - 60
    cw.save_task(raw_abandoned)
    raw_initializing = cw.load_task(abandoned_initializing["id"])
    raw_initializing["harness_expires_at"] = now - 100
    raw_initializing["status"] = "initializing"
    raw_initializing["agent_status"] = "idle"
    raw_initializing["updated_at"] = now - 60
    cw.save_task(raw_initializing)

    result = cw.cleanup_expired_harness_tasks(now=now)

    expected_purged = {
        terminal["id"],
        abandoned["id"],
        abandoned_initializing["id"],
        initialization_error["id"],
        *(task["id"] for task, _ in finalization_failures),
    }
    assert result["ok"] is True
    assert set(result["purged"]) == expected_purged
    assert result["failures"] == {}
    for task_id in expected_purged:
        with pytest.raises(HTTPException) as missing:
            cw.load_task(task_id)
        assert missing.value.status_code == 404
    assert cw.load_task(active["id"])["agent_status"] == "running"


def test_harness_deletion_refuses_an_active_validation(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="validation-delete-race",
        files={"app.py": "value\n"},
        prompt="Validate safely.",
        owner="test",
    )
    started = threading.Event()
    release = threading.Event()
    failures = []

    def fake_run_process(argv, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    def validate():
        try:
            cw.run_harness_validation_command(task["id"], argv=["python3", "-V"])
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "_run_process", fake_run_process)
    worker = threading.Thread(target=validate)
    worker.start()
    assert started.wait(timeout=5)
    try:
        with pytest.raises(HTTPException) as exc_info:
            cw.delete_harness_task(task["id"])
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert cw.delete_harness_task(task["id"])["ok"] is True


def test_harness_evidence_lease_blocks_mutation_until_atomic_delete(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="stable-evidence-snapshot",
        files={"app.py": "value\n"},
        prompt="Collect stable evidence.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)

    with pytest.raises(HTTPException) as run_start_error:
        cw.begin_harness_agent_run_start(task["id"])
    assert run_start_error.value.status_code == 409
    with pytest.raises(HTTPException) as tool_error:
        cw.begin_harness_agent_tool(task["id"])
    assert tool_error.value.status_code == 409
    for mutate in (
        lambda: cw.run_task_command(task["id"], argv=["python3", "-V"]),
        lambda: cw.git_diff(task["id"]),
        lambda: cw.search_text(task["id"], query="value"),
        lambda: cw.push_task(task["id"]),
        lambda: cw.create_pull_request(
            task["id"],
            title="must not create",
            body="",
        ),
        lambda: cw.append_guidance_message(
            task["id"],
            message="must not persist",
            actor="test",
        ),
        lambda: cw.set_task_coding_model(
            task["id"],
            coding_model="coder",
        ),
        lambda: cw.update_project_plan(
            task["id"],
            note="must not persist",
            actor="test",
        ),
        lambda: cw.replace_text(
            task["id"],
            path="app.py",
            old_text="value",
            new_text="changed",
        ),
        lambda: cw.write_file(task["id"], path="app.py", content="changed\n"),
        lambda: cw.apply_unified_patch(
            task["id"],
            patch=(
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1 +1 @@\n"
                "-value\n"
                "+changed\n"
            ),
        ),
        lambda: cw.commit_task(task["id"], message="must not commit"),
    ):
        with pytest.raises(HTTPException) as mutation_error:
            mutate()
        assert mutation_error.value.status_code == 409
    with pytest.raises(HTTPException) as validation_error:
        cw.run_harness_validation_command(task["id"], argv=["python3", "-V"])
    assert validation_error.value.status_code == 409
    with pytest.raises(HTTPException) as delete_error:
        cw.delete_harness_task(task["id"])
    assert delete_error.value.status_code == 409
    with pytest.raises(HTTPException) as generic_delete_error:
        cw.delete_task(task["id"])
    assert generic_delete_error.value.status_code == 403
    with pytest.raises(HTTPException) as archive_error:
        cw.archive_task(task["id"], actor="test", reason="must-not-move")
    assert archive_error.value.status_code == 403

    monkeypatch.setattr(
        cw,
        "_run_process",
        lambda argv, **kwargs: {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        },
    )
    validation = cw.run_harness_validation_command(
        task["id"],
        argv=["python3", "-V"],
        evidence_lease_id=lease["lease_id"],
    )
    assert validation["ok"] is True
    assert cw.read_harness_file_evidence(
        task["id"],
        path="app.py",
        evidence_lease_id=lease["lease_id"],
    )["content"] == "value\n"

    deleted = cw.delete_harness_task(
        task["id"],
        evidence_lease_id=lease["lease_id"],
    )
    assert deleted["ok"] is True
    assert lease["lease_id"] not in cw._ACTIVE_HARNESS_EVIDENCE_LEASES


def test_harness_evidence_requests_reject_stale_lease_after_restart(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="stale-evidence-lease",
        files={"app.py": "value\n"},
        prompt="Reject stale evidence after a restart.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)
    lease_id = lease["lease_id"]
    cw._ACTIVE_HARNESS_EVIDENCE_LEASES.clear()

    requests = (
        lambda: cw.harness_git_diff(task["id"], evidence_lease_id=lease_id),
        lambda: cw.harness_git_changes(task["id"], evidence_lease_id=lease_id),
        lambda: cw.read_harness_file_evidence(
            task["id"],
            path="app.py",
            evidence_lease_id=lease_id,
        ),
        lambda: cw.run_harness_validation_command(
            task["id"],
            argv=["python3", "-V"],
            evidence_lease_id=lease_id,
        ),
        lambda: cw.delete_harness_task(
            task["id"],
            evidence_lease_id=lease_id,
        ),
    )
    for request in requests:
        with pytest.raises(HTTPException) as stale_error:
            request()
        assert stale_error.value.status_code == 409
        assert stale_error.value.detail == "coding harness evidence lease is not active"

    assert cw.delete_harness_task(task["id"])["ok"] is True


def test_deleted_harness_tombstone_rejects_delayed_direct_save(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="delayed-direct-save",
        files={"app.py": "value\n"},
        prompt="Do not recreate deleted metadata.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    stale = cw.load_task(task["id"])
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)
    assert cw.delete_harness_task(
        task["id"],
        evidence_lease_id=lease["lease_id"],
    )["ok"] is True

    stale["agent_summary"] = "late notification save"
    with pytest.raises(HTTPException) as save_error:
        cw.save_task(stale)
    assert save_error.value.status_code == 409
    assert save_error.value.detail == "coding task has been deleted"
    assert not (tmp_path / "tasks" / f"{task['id']}.json").exists()


def test_harness_delete_serializes_with_direct_task_store_mutation(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="direct-store-delete-race",
        files={"app.py": "value\n"},
        prompt="Serialize direct task-store updates.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)
    started = threading.Event()
    release = threading.Event()
    deleted = threading.Event()
    failures = []

    def apply(current):
        started.set()
        assert release.wait(timeout=5)
        current["agent_summary"] = "direct store update completed"

    def mutate_directly():
        try:
            cw.mutate_task(task["id"], apply)
        except BaseException as exc:
            failures.append(exc)

    def delete_with_lease():
        try:
            cw.delete_harness_task(
                task["id"],
                evidence_lease_id=lease["lease_id"],
            )
            deleted.set()
        except BaseException as exc:
            failures.append(exc)

    mutation_worker = threading.Thread(target=mutate_directly)
    deletion_worker = threading.Thread(target=delete_with_lease)
    mutation_worker.start()
    assert started.wait(timeout=5)
    deletion_worker.start()
    assert deleted.wait(timeout=0.1) is False
    release.set()
    mutation_worker.join(timeout=5)
    deletion_worker.join(timeout=5)

    assert not mutation_worker.is_alive()
    assert not deletion_worker.is_alive()
    assert failures == []
    assert deleted.is_set()
    with pytest.raises(HTTPException) as missing:
        cw.load_task(task["id"])
    assert missing.value.status_code == 404


def test_harness_initialization_rejects_lease_and_delete_until_ready(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    initial_saved = threading.Event()
    release = threading.Event()
    failures = []
    created = []
    real_save_task = cw.save_task

    def blocking_initial_save(task):
        result = real_save_task(task)
        if (
            str(task.get("kind") or "") == "harness_eval"
            and str(task.get("status") or "") == "initializing"
            and not initial_saved.is_set()
        ):
            initial_saved.set()
            assert release.wait(timeout=5)
        return result

    def create():
        try:
            created.append(
                cw.create_harness_task(
                    fixture_id="initialization-delete-race",
                    files={"app.py": "value\n"},
                    prompt="Finish initialization before lifecycle operations.",
                    owner="test",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "save_task", blocking_initial_save)
    creator = threading.Thread(target=create)
    creator.start()
    assert initial_saved.wait(timeout=5)
    task_id = next(
        path.stem for path in (tmp_path / "tasks").glob("code_*.json")
    )
    assert not (tmp_path / "workspaces" / task_id).exists()

    with pytest.raises(HTTPException) as lease_error:
        cw.acquire_harness_evidence_lease(task_id, ttl_sec=300)
    assert lease_error.value.status_code == 409
    assert lease_error.value.detail == "coding harness task is still initializing"
    with pytest.raises(HTTPException) as delete_error:
        cw.delete_harness_task(task_id)
    assert delete_error.value.status_code == 409
    assert delete_error.value.detail == "coding task is still initializing"
    with pytest.raises(HTTPException) as run_start_error:
        cw.begin_harness_agent_run_start(task_id)
    assert run_start_error.value.status_code == 409
    assert run_start_error.value.detail == "coding harness task is still initializing"

    release.set()
    creator.join(timeout=5)
    assert not creator.is_alive()
    assert failures == []
    assert created[0]["status"] == "ready"
    assert (tmp_path / "workspaces" / task_id / "repo" / "app.py").is_file()

    cw.mutate_task(
        task_id,
        lambda current: current.update(agent_status="completed"),
    )
    lease = cw.acquire_harness_evidence_lease(task_id, ttl_sec=300)
    assert cw.delete_harness_task(
        task_id,
        evidence_lease_id=lease["lease_id"],
    )["ok"] is True
    assert not (tmp_path / "workspaces" / task_id).exists()


def test_expired_harness_evidence_lease_no_longer_blocks_resume(
    monkeypatch,
    tmp_path,
):
    _configure_roots(monkeypatch, tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(cw, "_now", lambda: clock["now"])
    task = cw.create_harness_task(
        fixture_id="expired-evidence-snapshot",
        files={"app.py": "value\n"},
        prompt="Allow recovery after a disconnected reader.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=30)
    lease_record = cw._ACTIVE_HARNESS_EVIDENCE_LEASES[lease["lease_id"]]
    evidence_root = Path(
        lease_record["workspace"]
    ).parent
    expiry_timer = lease_record["expiry_timer"]
    assert isinstance(expiry_timer, threading.Timer)
    assert expiry_timer.daemon is True
    expiry_timer.cancel()

    clock["now"] += 31
    cw._expire_harness_evidence_lease(lease["lease_id"], lease["expires_at"])
    registered = cw.begin_harness_agent_run_start(task["id"])
    cw.end_harness_agent_run_start(task["id"], registered=registered)

    assert registered is True
    assert lease["lease_id"] not in cw._ACTIVE_HARNESS_EVIDENCE_LEASES
    assert not evidence_root.exists()
    assert cw.delete_harness_task(task["id"])["ok"] is True


@pytest.mark.parametrize("operation_name", ["command", "push"])
def test_generic_persistent_harness_operation_blocks_evidence_lease_until_settled(
    monkeypatch,
    tmp_path,
    operation_name,
):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id=f"generic-{operation_name}-evidence-race",
        files={"app.py": "value\n"},
        prompt="Serialize a generic persistent operation with evidence.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="completed"),
    )
    started = threading.Event()
    release = threading.Event()
    failures = []

    def fake_run_process(argv, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "argv": list(argv),
            "duration_ms": 1,
        }

    def run_operation():
        try:
            if operation_name == "command":
                cw.run_task_command(task["id"], argv=["python3", "-V"])
            else:
                cw.push_task(task["id"])
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "_run_process", fake_run_process)
    worker = threading.Thread(target=run_operation)
    worker.start()
    assert started.wait(timeout=5)
    try:
        with pytest.raises(HTTPException) as exc_info:
            cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    lease = cw.acquire_harness_evidence_lease(task["id"], ttl_sec=300)
    assert cw.delete_harness_task(
        task["id"],
        evidence_lease_id=lease["lease_id"],
    )["ok"] is True


def test_harness_deletion_waits_for_cancelled_agent_tool_worker(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="agent-tool-delete-race",
        files={"app.py": "value\n"},
        prompt="Run a blocking read tool.",
        owner="test",
    )
    started = threading.Event()
    release = threading.Event()
    failures = []

    def fake_read_file(task_id, **kwargs):
        started.set()
        assert release.wait(timeout=30)
        stale = cw.load_task(task_id)
        stale["worker_finished"] = True
        cw.save_task(stale)
        return {"ok": True, "path": "app.py", "content": "value\n"}

    def run_tool():
        try:
            ca._run_tool_worker(
                task["id"],
                "coding_read_file",
                {"path": "app.py"},
                git_token_value=None,
            )
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "read_file", fake_read_file)
    worker = threading.Thread(target=run_tool)
    worker.start()
    assert started.wait(timeout=30)
    try:
        for collect in (
            lambda: cw.harness_git_diff(task["id"]),
            lambda: cw.harness_git_changes(task["id"]),
            lambda: cw.read_harness_file_evidence(task["id"], path="app.py"),
        ):
            with pytest.raises(HTTPException) as evidence_error:
                collect()
            assert evidence_error.value.status_code == 409
        with pytest.raises(HTTPException) as exc_info:
            cw.delete_harness_task(task["id"])
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert failures == []
    assert cw.load_task(task["id"])["worker_finished"] is True
    assert cw.delete_harness_task(task["id"])["ok"] is True


def test_harness_deletion_waits_for_automatic_checkpoint_worker(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="automatic-checkpoint-delete-race",
        files={"app.py": "before\n"},
        prompt="Modify and checkpoint the fixture.",
        owner="test",
    )
    repo = tmp_path / "workspaces" / task["id"] / "repo"
    (repo / "app.py").write_text("after\n", encoding="utf-8")
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="running"),
    )
    started = threading.Event()
    release = threading.Event()
    failures = []
    real_run_process = cw._run_process

    def blocking_run_process(argv, **kwargs):
        if list(argv) == ["git", "status", "--porcelain"]:
            started.set()
            assert release.wait(timeout=30)
        return real_run_process(argv, **kwargs)

    def checkpoint():
        try:
            ca._checkpoint_after_cycle(task["id"], run_id="run-1", cycle=3)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(cw, "_run_process", blocking_run_process)
    worker = threading.Thread(target=checkpoint)
    worker.start()
    assert started.wait(timeout=30)
    cw.mutate_task(
        task["id"],
        lambda current: current.update(
            agent_status="paused",
            stop_reason_code="run_timeout",
        ),
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            cw.delete_harness_task(task["id"])
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert failures == []
    saved = cw.load_task(task["id"])
    assert saved["agent_status"] == "paused"
    assert saved["stop_reason_code"] == "run_timeout"
    assert saved["last_checkpoint_run_id"] == "run-1"
    assert saved["last_checkpoint_cycle"] == 3
    assert cw.delete_harness_task(task["id"])["ok"] is True


def test_harness_deletion_rechecks_active_status_under_guard(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="active-delete-check",
        files={"app.py": "value\n"},
        prompt="Run the fixture.",
        owner="test",
    )
    cw.mutate_task(
        task["id"],
        lambda current: current.update(agent_status="queued"),
    )

    with pytest.raises(HTTPException) as exc_info:
        cw.delete_harness_task(task["id"])

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_harness_run_start_is_serialized_with_deletion(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="run-start-delete-race",
        files={"app.py": "value\n"},
        prompt="Start the fixture.",
        owner="test",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_start_impl(task_id, **kwargs):
        started.set()
        await release.wait()
        return {"id": task_id, "kind": "harness_eval", "agent_status": "idle"}

    monkeypatch.setattr(ca, "_start_agent_run_impl", fake_start_impl)
    run = asyncio.create_task(ca.start_agent_run(task["id"]))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        with pytest.raises(HTTPException) as exc_info:
            cw.delete_harness_task(task["id"])
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        await asyncio.wait_for(run, timeout=5)

    assert cw.delete_harness_task(task["id"])["ok"] is True


@pytest.mark.asyncio
async def test_failed_harness_start_repairs_orphaned_active_state(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="failed-run-start",
        files={"app.py": "value\n"},
        prompt="Start the fixture.",
        owner="test",
    )

    async def fail_after_queue(task_id, **kwargs):
        def mark_queued(current):
            current.update(
                agent_status="queued",
                agent_run_id="run-failed-start",
                agent_stop_requested=True,
                agent_pause_requested=True,
            )
            current["agent_runs"] = [
                {
                    "run_id": "run-failed-start",
                    "status": "queued",
                    "finished_at": None,
                }
            ]

        cw.mutate_task(task_id, mark_queued)
        raise RuntimeError("queue event failed")

    monkeypatch.setattr(ca, "_start_agent_run_impl", fail_after_queue)

    with pytest.raises(RuntimeError, match="queue event failed"):
        await ca.start_agent_run(task["id"])

    saved = cw.load_task(task["id"])
    assert saved["agent_status"] == "failed"
    assert saved["agent_error"] == "RuntimeError"
    assert saved["agent_stop_reason_code"] == "run_start_failed"
    assert saved["agent_stop_requested"] is False
    assert saved["agent_pause_requested"] is False
    assert saved["agent_runs"] == [
        {
            "run_id": "run-failed-start",
            "status": "failed",
            "finished_at": saved["agent_finished_at"],
            "summary": "Coding harness agent startup failed before runner registration.",
            "error": "RuntimeError",
            "stop_reason_code": "run_start_failed",
        }
    ]
    assert cw.delete_harness_task(task["id"])["ok"] is True


@pytest.mark.asyncio
async def test_cancelled_harness_start_waits_for_state_initialization(monkeypatch, tmp_path):
    _configure_roots(monkeypatch, tmp_path)
    task = cw.create_harness_task(
        fixture_id="cancelled-run-start",
        files={"app.py": "value\n"},
        prompt="Start the fixture.",
        owner="test",
    )
    initialize_started = threading.Event()
    release_initialize = threading.Event()
    original_initialize = ca._initialize_run_state

    def delayed_initialize(*args, **kwargs):
        initialize_started.set()
        assert release_initialize.wait(timeout=5)
        return original_initialize(*args, **kwargs)

    monkeypatch.setattr(ca, "_initialize_run_state", delayed_initialize)
    run = asyncio.create_task(ca.start_agent_run(task["id"]))
    assert await asyncio.to_thread(initialize_started.wait, 5)

    run.cancel()
    await asyncio.sleep(0)
    assert not run.done()
    release_initialize.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)

    saved = cw.load_task(task["id"])
    assert saved["agent_status"] == "failed"
    assert saved["agent_error"] == "CancelledError"
    assert saved["agent_stop_reason_code"] == "run_start_failed"
    assert saved["agent_runs"][0]["status"] == "failed"
    assert cw.delete_harness_task(task["id"])["ok"] is True


@pytest.mark.asyncio
async def test_harness_route_uses_durable_agent_runner(monkeypatch):
    user = SimpleNamespace(id=7, username="tester")
    captured = {}
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: user)
    monkeypatch.setattr(coding_routes, "_coding_model_for_user", lambda user, requested=None: requested or "coder")

    def fake_create(**kwargs):
        captured["create"] = kwargs
        return {"id": "code_abcdef123456", "kind": "harness_eval", "status": "ready"}

    async def fake_start(task_id, **kwargs):
        captured["start"] = {"task_id": task_id, **kwargs}
        return {
            "id": task_id,
            "kind": "harness_eval",
            "status": "ready",
            "agent": {"status": "queued"},
        }

    monkeypatch.setattr(coding_routes.cw, "create_harness_task", fake_create)
    monkeypatch.setattr(coding_routes.ca, "start_agent_run", fake_start)

    body = coding_routes.CodingHarnessRunRequest(
        fixture_id="small-fix",
        files={"app.py": "broken\n"},
        prompt="Fix app.py.",
        coding_model="coder",
        max_cycles=8,
        max_runtime_sec=120,
    )
    response = await coding_routes.v1_coding_harness_create_and_run(SimpleNamespace(), body)

    assert response["task"]["agent"]["status"] == "queued"
    assert captured["create"]["fixture_id"] == "small-fix"
    assert captured["create"]["files"] == {"app.py": "broken\n"}
    assert captured["start"]["task_id"] == "code_abcdef123456"
    assert captured["start"]["auto_commit"] is True


@pytest.mark.asyncio
async def test_harness_validation_route_uses_dedicated_runner(monkeypatch):
    captured = {}
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: SimpleNamespace(id=7))

    def fake_validation(task_id, **kwargs):
        captured.update({"task_id": task_id, **kwargs})
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(coding_routes.cw, "run_harness_validation_command", fake_validation)
    response = await coding_routes.v1_coding_harness_validation(
        SimpleNamespace(),
        "code_abcdef123456",
        coding_routes.CodingCommandRequest(
            argv=["python3", "-m", "unittest"],
            timeout_sec=250,
        ),
        evidence_lease_id="lease-test-123",
    )

    assert response == {"result": {"ok": True, "returncode": 0}}
    assert captured == {
        "task_id": "code_abcdef123456",
        "argv": ["python3", "-m", "unittest"],
        "cwd": None,
        "timeout_sec": 250,
        "evidence_lease_id": "lease-test-123",
    }


@pytest.mark.asyncio
async def test_harness_evidence_routes_require_and_forward_lease(monkeypatch):
    captured = []
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(
        coding_routes.cw,
        "harness_git_diff",
        lambda task_id, **kwargs: captured.append(("diff", task_id, kwargs))
        or {"ok": True},
    )
    monkeypatch.setattr(
        coding_routes.cw,
        "harness_git_changes",
        lambda task_id, **kwargs: captured.append(("changes", task_id, kwargs))
        or {"ok": True},
    )
    monkeypatch.setattr(
        coding_routes.cw,
        "read_harness_file_evidence",
        lambda task_id, **kwargs: captured.append(("file", task_id, kwargs))
        or {"content": "value\n"},
    )

    await coding_routes.v1_coding_harness_diff(
        SimpleNamespace(),
        "code_abcdef123456",
        evidence_lease_id="lease-test-123",
    )
    await coding_routes.v1_coding_harness_changes(
        SimpleNamespace(),
        "code_abcdef123456",
        evidence_lease_id="lease-test-123",
    )
    await coding_routes.v1_coding_harness_file(
        SimpleNamespace(),
        "code_abcdef123456",
        path="app.py",
        evidence_lease_id="lease-test-123",
    )

    assert captured == [
        (
            "diff",
            "code_abcdef123456",
            {"evidence_lease_id": "lease-test-123"},
        ),
        (
            "changes",
            "code_abcdef123456",
            {"evidence_lease_id": "lease-test-123"},
        ),
        (
            "file",
            "code_abcdef123456",
            {"path": "app.py", "evidence_lease_id": "lease-test-123"},
        ),
    ]


@pytest.mark.asyncio
async def test_harness_evidence_lease_routes_delegate_to_guarded_workspace(
    monkeypatch,
):
    captured = []
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(
        coding_routes.cw,
        "acquire_harness_evidence_lease",
        lambda task_id, **kwargs: captured.append(("acquire", task_id, kwargs))
        or {"lease_id": "lease-test-123", "task_id": task_id},
    )
    monkeypatch.setattr(
        coding_routes.cw,
        "release_harness_evidence_lease",
        lambda task_id, **kwargs: captured.append(("release", task_id, kwargs))
        or {"ok": True, "lease_id": kwargs["lease_id"], "task_id": task_id},
    )

    acquired = await coding_routes.v1_coding_harness_acquire_evidence_lease(
        SimpleNamespace(),
        "code_abcdef123456",
        coding_routes.CodingHarnessEvidenceLeaseRequest(ttl_sec=450),
    )
    released = await coding_routes.v1_coding_harness_release_evidence_lease(
        SimpleNamespace(),
        "code_abcdef123456",
        "lease-test-123",
    )

    assert acquired["lease"]["lease_id"] == "lease-test-123"
    assert released["result"]["ok"] is True
    assert captured == [
        ("acquire", "code_abcdef123456", {"ttl_sec": 450.0}),
        ("release", "code_abcdef123456", {"lease_id": "lease-test-123"}),
    ]


@pytest.mark.asyncio
async def test_harness_evidence_lease_route_waits_for_live_runner(monkeypatch):
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: True)
    monkeypatch.setattr(
        coding_routes.cw,
        "acquire_harness_evidence_lease",
        lambda *args, **kwargs: pytest.fail("lease must wait for the live runner"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await coding_routes.v1_coding_harness_acquire_evidence_lease(
            SimpleNamespace(),
            "code_abcdef123456",
            coding_routes.CodingHarnessEvidenceLeaseRequest(ttl_sec=300),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_harness_route_deletes_workspace_when_agent_start_fails(monkeypatch):
    deleted = []
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(
        coding_routes.cw,
        "create_harness_task",
        lambda **kwargs: {"id": "code_abcdef123456", "kind": "harness_eval", "status": "ready"},
    )

    async def fail_start(task_id, **kwargs):
        raise RuntimeError("could not queue")

    monkeypatch.setattr(coding_routes.ca, "start_agent_run", fail_start)
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(
        coding_routes.cw,
        "delete_harness_task",
        lambda task_id: deleted.append(task_id) or {"ok": True},
    )

    body = coding_routes.CodingHarnessRunRequest(
        fixture_id="small-fix",
        files={"app.py": "broken\n"},
        prompt="Fix app.py.",
    )
    with pytest.raises(RuntimeError, match="could not queue"):
        await coding_routes.v1_coding_harness_create_and_run(SimpleNamespace(), body)

    assert deleted == ["code_abcdef123456"]


@pytest.mark.asyncio
async def test_harness_delete_route_is_scoped_to_terminal_harness_tasks(monkeypatch):
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(
        coding_routes.cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "harness_eval", "agent_status": "completed"},
    )
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(
        coding_routes.cw,
        "delete_harness_task",
        lambda task_id, **kwargs: {
            "ok": True,
            "task_id": task_id,
            "evidence_lease_id": kwargs.get("evidence_lease_id"),
        },
    )

    response = await coding_routes.v1_coding_harness_delete_task(
        SimpleNamespace(),
        "code_abcdef123456",
        evidence_lease_id="lease-test-123",
    )

    assert response["result"] == {
        "ok": True,
        "task_id": "code_abcdef123456",
        "evidence_lease_id": "lease-test-123",
    }


@pytest.mark.asyncio
async def test_harness_delete_route_rejects_active_or_normal_tasks(monkeypatch):
    monkeypatch.setattr(coding_routes, "_require_coding_api", lambda req: None)
    monkeypatch.setattr(coding_routes.ca, "agent_run_active", lambda task_id: False)
    monkeypatch.setattr(
        coding_routes.cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "workspace", "agent_status": "completed"},
    )
    with pytest.raises(HTTPException) as normal_exc:
        await coding_routes.v1_coding_harness_delete_task(SimpleNamespace(), "code_abcdef123456")
    assert normal_exc.value.status_code == 403

    monkeypatch.setattr(
        coding_routes.cw,
        "load_task",
        lambda task_id: {"id": task_id, "kind": "harness_eval", "agent_status": "running"},
    )
    with pytest.raises(HTTPException) as active_exc:
        await coding_routes.v1_coding_harness_delete_task(SimpleNamespace(), "code_abcdef123456")
    assert active_exc.value.status_code == 409
