from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from app import coding_agent as ca
from app import coding_model_policy
from app import coding_workspace as cw
from app.config import S, logger


SCHEMA = "nexus_coding_agent_smoke.v1"
DIFF_CHECK_ARGV = ["git", "diff", "--check"]
TERMINAL_AGENT_STATUSES = {"completed", "failed", "paused", "stopped", "idle_waiting"}


@dataclass(frozen=True)
class SmokeProfile:
    profile_id: str
    label: str
    complexity: str
    fixture_dir: str
    goal: str
    expected_changes: tuple[str, ...]
    protected_files: tuple[str, ...]
    verify_argv: tuple[str, ...]
    commit_message: str


def _unittest_argv(fixture_dir: str) -> tuple[str, ...]:
    return ("python", "-m", "unittest", "discover", "-s", fixture_dir, "-p", "verify_*.py")


SMOKE_PROFILES: Dict[str, SmokeProfile] = {
    "fixture_median": SmokeProfile(
        profile_id="fixture_median",
        label="Fixture median repair",
        complexity="simple",
        fixture_dir="fixtures/coding-smoke-project",
        goal="Fix summarize_numbers so it returns the correct median for even-length inputs.",
        expected_changes=("fixtures/coding-smoke-project/math_tools.py",),
        protected_files=("fixtures/coding-smoke-project/verify_behavior.py",),
        verify_argv=_unittest_argv("fixtures/coding-smoke-project"),
        commit_message="Nexus coding smoke: fix fixture median",
    ),
    "fixture_inventory": SmokeProfile(
        profile_id="fixture_inventory",
        label="Fixture inventory aggregation",
        complexity="medium",
        fixture_dir="fixtures/coding-smoke-inventory",
        goal="Fix build_reorder_plan so it normalizes SKUs, aggregates duplicate rows, and returns sorted reorder quantities.",
        expected_changes=("fixtures/coding-smoke-inventory/inventory_tools.py",),
        protected_files=("fixtures/coding-smoke-inventory/verify_behavior.py",),
        verify_argv=_unittest_argv("fixtures/coding-smoke-inventory"),
        commit_message="Nexus coding smoke: fix fixture inventory aggregation",
    ),
    "fixture_route_flags": SmokeProfile(
        profile_id="fixture_route_flags",
        label="Fixture route flags",
        complexity="moderate",
        fixture_dir="fixtures/coding-smoke-routing",
        goal="Fix feature flag parsing and route resolution so enabled routes are normalized and missing routes return not_found.",
        expected_changes=(
            "fixtures/coding-smoke-routing/feature_flags.py",
            "fixtures/coding-smoke-routing/router.py",
        ),
        protected_files=("fixtures/coding-smoke-routing/verify_behavior.py",),
        verify_argv=_unittest_argv("fixtures/coding-smoke-routing"),
        commit_message="Nexus coding smoke: fix fixture route flags",
    ),
}

_scheduler_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_run_lock = asyncio.Lock()


class SmokeFailure(RuntimeError):
    def __init__(self, message: str, *, report: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


def _csv(raw: str, *, fallback: str = "") -> List[str]:
    value = str(raw or fallback or "")
    return [item.strip() for item in value.split(",") if item.strip()]


def _report_dir() -> Path:
    return Path(str(getattr(S, "CODING_SMOKE_REPORT_DIR", "") or "/var/lib/gateway/coding_smoke_reports")).resolve()


def _profile(profile_id: str) -> SmokeProfile:
    key = str(profile_id or "fixture_median").strip() or "fixture_median"
    if key not in SMOKE_PROFILES:
        raise SmokeFailure(f"unknown smoke profile '{key}'")
    return SMOKE_PROFILES[key]


def _prompt(profile: SmokeProfile) -> str:
    protected = "\n".join(f"- `{path}`" for path in profile.protected_files)
    expected = "\n".join(f"- `{path}`" for path in profile.expected_changes)
    verify = " ".join(profile.verify_argv)
    return f"""Nexus Coding framework smoke test.

Goal: make the fixture project pass its validation command.

Scope:
- Work only in `{profile.fixture_dir}`.
- {profile.goal}
- Do not edit these protected validation files:
{protected}
- Do not push a branch or open a pull request.

Required validation before calling coding_finish:
- `{verify}`
- `git diff --check`

Success criteria:
- The unittest command passes.
- `git diff --check` passes.
- The diff only changes these allowed implementation files:
{expected}
- Call `coding_finish` with a concise summary after checking the diff.
"""


def _append_phase(report: Dict[str, Any], name: str, ok: bool, **details: Any) -> None:
    item = {"name": name, "ok": bool(ok), "ts": int(time.time())}
    item.update(details)
    report.setdefault("phases", []).append(item)


def _agent_status(task: Dict[str, Any]) -> str:
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    return str(agent.get("status") or "idle").strip().lower()


def _command_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": bool(payload.get("ok")),
        "returncode": payload.get("returncode"),
        "stdout_tail": str(payload.get("stdout") or "")[-2000:],
        "stderr_tail": str(payload.get("stderr") or "")[-2000:],
    }


def _changed_files(diff_payload: Dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("changes", "committed_changes"):
        value = diff_payload.get(key)
        files = value.get("files") if isinstance(value, dict) else []
        if isinstance(files, list):
            for item in files:
                if isinstance(item, dict) and item.get("path"):
                    out.add(str(item["path"]))
    return out


def _write_report(report: Dict[str, Any]) -> None:
    target_dir = _report_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(report.get("task_id") or "no-task")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    path = target_dir / f"coding-smoke-{stamp}-{task_id}.json"
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def _verify(task_id: str, profile: SmokeProfile, report: Dict[str, Any]) -> None:
    verify = cw.run_task_command(task_id, argv=list(profile.verify_argv), timeout_sec=120)
    report["validation"] = _command_summary(verify)
    _append_phase(report, "validation", bool(verify.get("ok")), argv=list(profile.verify_argv))
    if not verify.get("ok"):
        raise SmokeFailure("fixture validation command failed", report=report)

    diff_check = cw.run_task_command(task_id, argv=DIFF_CHECK_ARGV, timeout_sec=60)
    report["diff_check"] = _command_summary(diff_check)
    _append_phase(report, "diff_check", bool(diff_check.get("ok")), argv=DIFF_CHECK_ARGV)
    if not diff_check.get("ok"):
        raise SmokeFailure("git diff --check failed", report=report)

    changed = _changed_files(cw.git_diff(task_id))
    report["changed_files"] = sorted(changed)
    _append_phase(report, "diff_audit", True, changed_files=sorted(changed))
    missing = sorted(path for path in profile.expected_changes if path not in changed)
    if missing:
        raise SmokeFailure(f"expected changed file not found: {', '.join(missing)}", report=report)
    disallowed = sorted(path for path in changed if path not in set(profile.expected_changes))
    if disallowed:
        raise SmokeFailure(f"unexpected changed files: {', '.join(disallowed)}", report=report)
    protected = sorted(path for path in changed if path in set(profile.protected_files))
    if protected:
        raise SmokeFailure(f"protected test file was modified: {', '.join(protected)}", report=report)


async def run_one(*, model: str, profile_id: str) -> Dict[str, Any]:
    started_at = int(time.time())
    branch_name = f"nexus-coding-smoke/{started_at}-{secrets.token_hex(3)}"
    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "profile_id": str(profile_id or ""),
        "profile_label": str(profile_id or ""),
        "complexity": "",
        "fixture_dir": "",
        "expected_changes": [],
        "protected_files": [],
        "model": model,
        "started_at": started_at,
        "branch_name": branch_name,
        "task_id": "",
        "phases": [],
        "interventions": [],
    }
    profile = _profile(profile_id)
    report.update(
        {
            "profile_id": profile.profile_id,
            "profile_label": profile.label,
            "complexity": profile.complexity,
            "fixture_dir": profile.fixture_dir,
            "expected_changes": list(profile.expected_changes),
            "protected_files": list(profile.protected_files),
        }
    )

    def fail(message: str) -> None:
        raise SmokeFailure(message, report=report)

    try:
        policy = coding_model_policy.describe_workspace_model(model)
        if str(policy.get("run_policy") or "") == "idle_only":
            report["skipped"] = True
            fail(str(policy.get("warning") or "huge model is not currently loaded"))

        task = await asyncio.to_thread(
            cw.create_task,
            repo_url=None,
            base_branch=None,
            branch_name=branch_name,
            prompt=_prompt(profile),
            owner="coding-smoke-scheduler",
            owner_user_id=None,
            git_token_value=None,
            coding_model=model,
        )
        task_id = str(task.get("id") or "")
        report["task_id"] = task_id
        report["repo_url"] = str(task.get("repo_url") or "")
        _append_phase(report, "create", bool(task_id) and str(task.get("status") or "") != "error", task_id=task_id)
        if not task_id:
            fail("coding run did not return a task id")
        if str(task.get("status") or "") == "error":
            fail(f"workspace creation failed: {task.get('error') or task}")

        task = await ca.start_agent_run(
            task_id,
            coding_model=model,
            auto_commit=True,
            commit_message=profile.commit_message,
            actor="coding-smoke-scheduler",
        )
        _append_phase(report, "start_agent", _agent_status(task) not in {"failed", "idle_waiting"}, agent_status=_agent_status(task))

        deadline = time.monotonic() + max(1.0, float(getattr(S, "CODING_SMOKE_TIMEOUT_SEC", 1200.0) or 1200.0))
        poll = max(1.0, float(getattr(S, "CODING_SMOKE_POLL_SEC", 10.0) or 10.0))
        last_task = task
        last_inspect: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_task = await asyncio.to_thread(lambda: cw.public_task(cw.load_task(task_id)))
            status = _agent_status(last_task)
            last_inspect = await asyncio.to_thread(cw.inspect_task, task_id, stalled_after_sec=float(getattr(S, "CODING_SMOKE_STALLED_AFTER_SEC", 180.0) or 180.0))
            report["last_agent_status"] = status
            inspect_task = last_inspect.get("task") if isinstance(last_inspect.get("task"), dict) else {}
            report["last_attention"] = inspect_task.get("attention") if isinstance(inspect_task, dict) else []
            if status in TERMINAL_AGENT_STATUSES:
                break
            await asyncio.sleep(poll)
        else:
            await ca.request_pause(task_id)
            fail(f"coding run timed out after {float(getattr(S, 'CODING_SMOKE_TIMEOUT_SEC', 1200.0) or 1200.0):.0f}s")

        final_status = _agent_status(last_task)
        report["final_task"] = {
            "status": last_task.get("status"),
            "agent_status": final_status,
            "agent_summary": ((last_task.get("agent") or {}).get("summary") if isinstance(last_task.get("agent"), dict) else ""),
            "agent_error": ((last_task.get("agent") or {}).get("error") if isinstance(last_task.get("agent"), dict) else ""),
        }
        agent = last_task.get("agent") if isinstance(last_task.get("agent"), dict) else {}
        report["backend"] = agent.get("backend") or ""
        report["upstream_model"] = agent.get("upstream_model") or ""
        report["agent_elapsed_runtime_sec"] = agent.get("elapsed_runtime_sec")
        report["final_inspect"] = last_inspect.get("task") if isinstance(last_inspect.get("task"), dict) else last_inspect
        _append_phase(report, "agent_terminal", final_status == "completed", agent_status=final_status)
        if final_status != "completed":
            fail(f"agent did not complete successfully: {final_status}")

        await asyncio.to_thread(_verify, task_id, profile, report)
        report["ok"] = True
        report["finished_at"] = int(time.time())
        report["duration_sec"] = int(report["finished_at"] - started_at)
        archive = await asyncio.to_thread(cw.archive_task, task_id, actor="coding-smoke-scheduler", reason="smoke_archive")
        report["archive"] = {"ok": bool(archive.get("ok")), "archive_id": archive.get("archive_id") or ""}
        _append_phase(report, "archive", bool(archive.get("ok")), archive_id=archive.get("archive_id") or "")
        return report
    except SmokeFailure as exc:
        report = exc.report if isinstance(getattr(exc, "report", None), dict) else report
        report["ok"] = False
        report["error"] = str(exc)
        report["finished_at"] = int(time.time())
        report["duration_sec"] = int(report["finished_at"] - started_at)
        return report
    except Exception as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["finished_at"] = int(time.time())
        report["duration_sec"] = int(report["finished_at"] - started_at)
        return report
    finally:
        try:
            _write_report(report)
        except Exception as exc:
            logger.warning("coding smoke report write failed: %s: %s", type(exc).__name__, exc)


def _weekly_idle_window() -> bool:
    now = time.localtime()
    day = int(getattr(S, "CODING_SMOKE_WEEKLY_DAY", 7) or 7)
    start = int(getattr(S, "CODING_SMOKE_IDLE_START_HOUR", 0) or 0)
    end = int(getattr(S, "CODING_SMOKE_IDLE_END_HOUR", 6) or 6)
    return now.tm_wday + 1 == day and start <= now.tm_hour < end


async def run_suite() -> None:
    if _run_lock.locked():
        logger.info("coding smoke suite already running; skipping")
        return
    async with _run_lock:
        profiles = _csv(getattr(S, "CODING_SMOKE_PROFILES", ""), fallback="fixture_median,fixture_inventory,fixture_route_flags")
        for model in _csv(getattr(S, "CODING_SMOKE_MODELS", ""), fallback="coder"):
            for profile_id in profiles:
                logger.info("coding smoke start model=%s profile=%s", model, profile_id)
                report = await run_one(model=model, profile_id=profile_id)
                logger.info("coding smoke finished model=%s profile=%s ok=%s task=%s", model, profile_id, report.get("ok"), report.get("task_id"))

        weekly_models = _csv(getattr(S, "CODING_SMOKE_WEEKLY_MODELS", ""))
        if weekly_models and _weekly_idle_window():
            weekly_profiles = _csv(getattr(S, "CODING_SMOKE_WEEKLY_PROFILES", ""), fallback=",".join(profiles))
            for model in weekly_models:
                for profile_id in weekly_profiles:
                    logger.info("coding smoke weekly start model=%s profile=%s", model, profile_id)
                    report = await run_one(model=model, profile_id=profile_id)
                    logger.info("coding smoke weekly finished model=%s profile=%s ok=%s task=%s", model, profile_id, report.get("ok"), report.get("task_id"))
        elif weekly_models:
            logger.info("coding smoke weekly models skipped outside idle window")


async def _loop(stop: asyncio.Event) -> None:
    interval = max(60.0, float(getattr(S, "CODING_SMOKE_START_INTERVAL_SEC", 3600.0) or 3600.0))
    run_at_load = bool(getattr(S, "CODING_SMOKE_RUN_AT_STARTUP", True))
    if run_at_load:
        await run_suite()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            await run_suite()


async def start_scheduler() -> None:
    global _scheduler_task, _stop_event
    if not bool(getattr(S, "CODING_SMOKE_SCHEDULER_ENABLED", False)):
        return
    if _scheduler_task and not _scheduler_task.done():
        return
    _stop_event = asyncio.Event()
    _scheduler_task = asyncio.create_task(_loop(_stop_event))
    logger.info("coding smoke scheduler started")


async def stop_scheduler() -> None:
    global _scheduler_task, _stop_event
    task = _scheduler_task
    stop = _stop_event
    if stop is not None:
        stop.set()
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _scheduler_task = None
    _stop_event = None
