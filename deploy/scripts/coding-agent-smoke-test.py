#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA = "nexus_coding_agent_smoke.v1"
FIXTURE_DIR = "fixtures/coding-smoke-project"
EXPECTED_CHANGE = f"{FIXTURE_DIR}/math_tools.py"
PROTECTED_TEST = f"{FIXTURE_DIR}/verify_behavior.py"
VERIFY_ARGV = ["python", "-m", "unittest", "discover", "-s", FIXTURE_DIR, "-p", "verify_*.py"]
DIFF_CHECK_ARGV = ["git", "diff", "--check"]
TERMINAL_AGENT_STATUSES = {"completed", "failed", "paused", "stopped", "idle_waiting"}
ACTIVE_AGENT_STATUSES = {"queued", "running", "stopping", "pausing"}

SMOKE_PROMPT = f"""Nexus Coding framework smoke test.

Goal: make the small fixture project pass its validation command.

Scope:
- Work only in `{FIXTURE_DIR}`.
- Fix `{EXPECTED_CHANGE}` so `summarize_numbers` returns the correct median for even-length inputs.
- Do not edit `{PROTECTED_TEST}`.
- Do not push a branch or open a pull request.

Required validation before calling coding_finish:
- `python -m unittest discover -s {FIXTURE_DIR} -p "verify_*.py"`
- `git diff --check`

Success criteria:
- The unittest command passes.
- `git diff --check` passes.
- The diff only changes `{EXPECTED_CHANGE}`.
- Call `coding_finish` with a concise summary after checking the diff.
"""

INTERVENTION_MESSAGE = f"""Continue the Nexus Coding smoke test.

Use the workspace tools, not prose-only responses. Inspect `{FIXTURE_DIR}`, fix `{EXPECTED_CHANGE}`,
run `python -m unittest discover -s {FIXTURE_DIR} -p "verify_*.py"` and `git diff --check`,
inspect the diff, then call coding_finish. Do not edit `{PROTECTED_TEST}`.
"""


class SmokeFailure(RuntimeError):
    def __init__(self, message: str, *, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


class GatewayClient:
    def __init__(self, base_url: str, token: str, *, timeout_sec: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_sec = timeout_sec

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            clean_query = {k: v for k, v in query.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail: Any = body
            try:
                detail = json.loads(body)
            except Exception:
                pass
            raise SmokeFailure(f"{method.upper()} {path} failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SmokeFailure(f"{method.upper()} {path} failed: {exc}") from exc

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, payload=payload or {})


def parse_env_file(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        raise SmokeFailure(f"env file not found: {path}")
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def choose_token(args: argparse.Namespace, env_file_values: dict[str, str]) -> str:
    raw = (
        args.token
        or os.environ.get("NEXUS_CODING_SMOKE_TOKEN")
        or os.environ.get("GATEWAY_BEARER_TOKEN")
        or env_file_values.get("GATEWAY_BEARER_TOKENS")
        or env_file_values.get("GATEWAY_BEARER_TOKEN")
        or ""
    )
    for item in str(raw).split(","):
        token = item.strip()
        if token:
            return token
    raise SmokeFailure("no bearer token configured; pass --token, NEXUS_CODING_SMOKE_TOKEN, or --env-file")


def redact_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return str(url or "")


def changed_files_from_diff(diff_payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("changes", "committed_changes"):
        value = diff_payload.get(key)
        files = value.get("files") if isinstance(value, dict) else []
        if isinstance(files, list):
            for item in files:
                if isinstance(item, dict) and item.get("path"):
                    out.add(str(item["path"]))
    return out


def command_ok(payload: dict[str, Any]) -> bool:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    return bool(isinstance(result, dict) and result.get("ok"))


def command_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if not isinstance(result, dict):
        return {"ok": False, "error": "missing command result"}
    return {
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "stdout_tail": str(result.get("stdout") or "")[-2000:],
        "stderr_tail": str(result.get("stderr") or "")[-2000:],
    }


def agent_status(task: dict[str, Any]) -> str:
    agent = task.get("agent") if isinstance(task.get("agent"), dict) else {}
    return str(agent.get("status") or "idle").strip().lower()


def append_phase(report: dict[str, Any], name: str, ok: bool, **details: Any) -> None:
    item = {"name": name, "ok": bool(ok), "ts": int(time.time())}
    item.update(details)
    report.setdefault("phases", []).append(item)


def verify_final_state(client: GatewayClient, task_id: str, allowed_changes: set[str], report: dict[str, Any]) -> None:
    verify = client.post(
        f"/v1/coding/tasks/{urllib.parse.quote(task_id)}/command",
        {"argv": VERIFY_ARGV, "timeout_sec": 120},
    )
    report["validation"] = command_summary(verify)
    append_phase(report, "validation", command_ok(verify), argv=VERIFY_ARGV)
    if not command_ok(verify):
        raise SmokeFailure("fixture validation command failed", report=report)

    diff_check = client.post(
        f"/v1/coding/tasks/{urllib.parse.quote(task_id)}/command",
        {"argv": DIFF_CHECK_ARGV, "timeout_sec": 60},
    )
    report["diff_check"] = command_summary(diff_check)
    append_phase(report, "diff_check", command_ok(diff_check), argv=DIFF_CHECK_ARGV)
    if not command_ok(diff_check):
        raise SmokeFailure("git diff --check failed", report=report)

    diff_payload = client.get(f"/v1/coding/tasks/{urllib.parse.quote(task_id)}/diff")
    changed = changed_files_from_diff(diff_payload)
    report["changed_files"] = sorted(changed)
    append_phase(report, "diff_audit", True, changed_files=sorted(changed))

    if EXPECTED_CHANGE not in changed:
        raise SmokeFailure(f"expected changed file not found: {EXPECTED_CHANGE}", report=report)
    disallowed = sorted(path for path in changed if path not in allowed_changes)
    if disallowed:
        raise SmokeFailure(f"unexpected changed files: {', '.join(disallowed)}", report=report)
    if PROTECTED_TEST in changed:
        raise SmokeFailure(f"protected test file was modified: {PROTECTED_TEST}", report=report)


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started_at = int(time.time())
    branch_suffix = f"{started_at}-{secrets.token_hex(3)}"
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "profile_id": args.profile_id,
        "profile_label": args.profile_label,
        "complexity": args.complexity,
        "model": args.model,
        "started_at": started_at,
        "base_url": args.base_url.rstrip("/"),
        "task_id": "",
        "branch_name": "",
        "repo_url": "",
        "phases": [],
        "interventions": [],
    }

    def fail(message: str) -> None:
        raise SmokeFailure(message, report=report)

    try:
        env_file_values = parse_env_file(args.env_file)
        token = choose_token(args, env_file_values)
    except SmokeFailure as exc:
        raise SmokeFailure(str(exc), report=report) from exc
    client = GatewayClient(args.base_url, token, timeout_sec=args.http_timeout_sec)

    config = client.get("/v1/coding/config")
    append_phase(report, "config", True)
    if not bool(config.get("enabled")):
        fail("coding is disabled")
    if not bool(config.get("bearer_api_enabled")):
        fail("coding bearer API is disabled")

    repo_url = args.repo_url or str(config.get("default_repo_url") or "").strip()
    base_branch = args.base_branch or str(config.get("default_base_branch") or "main").strip() or "main"
    branch_name = args.branch_name or f"{args.branch_prefix.rstrip('/')}/{branch_suffix}"
    report["repo_url"] = redact_url(repo_url)
    report["branch_name"] = branch_name

    tools = client.get("/v1/coding/tools")
    append_phase(report, "tools", bool(tools))

    create_payload = {
        "repo_url": repo_url,
        "base_branch": base_branch,
        "branch_name": branch_name,
        "prompt": args.prompt or SMOKE_PROMPT,
        "coding_model": args.model,
        "auto_commit": True,
        "commit_message": "Nexus coding smoke: fix fixture median",
    }
    created = client.post("/v1/coding/runs", create_payload)
    task = created.get("task") if isinstance(created.get("task"), dict) else {}
    task_id = str(task.get("id") or "")
    if not task_id:
        fail("coding run did not return a task id")
    report["task_id"] = task_id
    append_phase(report, "create_and_run", str(task.get("status") or "") != "error", task_id=task_id)
    if str(task.get("status") or "") == "error":
        fail(f"workspace creation failed: {task.get('error') or task}")

    deadline = time.monotonic() + args.timeout_sec
    interventions = 0
    last_inspect: dict[str, Any] = {}
    last_task: dict[str, Any] = task

    while time.monotonic() < deadline:
        last_task = client.get(f"/v1/coding/tasks/{urllib.parse.quote(task_id)}").get("task", {})
        status = agent_status(last_task)
        last_inspect = client.get(
            f"/v1/coding/tasks/{urllib.parse.quote(task_id)}/inspect",
            {"stalled_after_sec": args.stalled_after_sec},
        )
        inspect_task = last_inspect.get("task") if isinstance(last_inspect.get("task"), dict) else {}
        report["last_agent_status"] = status
        report["last_attention"] = inspect_task.get("attention") if isinstance(inspect_task, dict) else []

        if status in TERMINAL_AGENT_STATUSES:
            break

        if (
            args.auto_intervene
            and interventions < args.max_interventions
            and isinstance(inspect_task, dict)
            and inspect_task.get("needs_attention")
            and str(inspect_task.get("recommended_action") or "")
        ):
            action = str(inspect_task.get("recommended_action") or "guidance")
            if action not in {"guidance", "guide_and_resume", "resume"}:
                action = "guidance"
            intervention = client.post(
                f"/v1/coding/tasks/{urllib.parse.quote(task_id)}/intervene",
                {
                    "action": action,
                    "message": INTERVENTION_MESSAGE,
                    "actor": "coding-smoke-harness",
                    "coding_model": args.model,
                    "auto_commit": True,
                    "commit_message": "Nexus coding smoke: fix fixture median",
                },
            )
            interventions += 1
            report["interventions"].append(
                {
                    "action": action,
                    "ok": bool(intervention.get("ok")),
                    "attention": inspect_task.get("attention"),
                    "ts": int(time.time()),
                }
            )

        time.sleep(args.poll_sec)
    else:
        try:
            client.post(
                f"/v1/coding/tasks/{urllib.parse.quote(task_id)}/intervene",
                {"action": "pause", "actor": "coding-smoke-harness"},
            )
        except Exception:
            pass
        report["final_task"] = {
            "status": last_task.get("status"),
            "agent_status": agent_status(last_task),
            "agent_summary": ((last_task.get("agent") or {}).get("summary") if isinstance(last_task.get("agent"), dict) else ""),
            "agent_error": ((last_task.get("agent") or {}).get("error") if isinstance(last_task.get("agent"), dict) else ""),
        }
        report["final_inspect"] = last_inspect.get("task") if isinstance(last_inspect.get("task"), dict) else last_inspect
        fail(f"coding run timed out after {args.timeout_sec:.0f}s")

    final_status = agent_status(last_task)
    report["final_task"] = {
        "status": last_task.get("status"),
        "agent_status": final_status,
        "agent_summary": ((last_task.get("agent") or {}).get("summary") if isinstance(last_task.get("agent"), dict) else ""),
        "agent_error": ((last_task.get("agent") or {}).get("error") if isinstance(last_task.get("agent"), dict) else ""),
    }
    agent_payload = last_task.get("agent") if isinstance(last_task.get("agent"), dict) else {}
    report["backend"] = agent_payload.get("backend") or ""
    report["upstream_model"] = agent_payload.get("upstream_model") or ""
    report["agent_elapsed_runtime_sec"] = agent_payload.get("elapsed_runtime_sec")
    report["final_inspect"] = last_inspect.get("task") if isinstance(last_inspect.get("task"), dict) else last_inspect
    append_phase(report, "agent_terminal", final_status == "completed", agent_status=final_status)
    if final_status != "completed":
        fail(f"agent did not complete successfully: {final_status}")

    allowed_changes = set(args.allowed_change or [EXPECTED_CHANGE])
    verify_final_state(client, task_id, allowed_changes, report)
    report["ok"] = True
    report["finished_at"] = int(time.time())
    report["duration_sec"] = int(report["finished_at"] - started_at)
    return report


def write_report(report: dict[str, Any], output_dir: str | None) -> None:
    if not output_dir:
        return
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(report.get("task_id") or "no-task")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    path = target_dir / f"coding-smoke-{stamp}-{task_id}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Nexus Coding agent smoke test through the live Coding API.")
    parser.add_argument("--base-url", default=os.environ.get("NEXUS_GATEWAY_URL", "http://127.0.0.1:8800"))
    parser.add_argument("--token", default="")
    parser.add_argument("--env-file", default=os.environ.get("NEXUS_ENV_FILE", ""))
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--base-branch", default="")
    parser.add_argument("--branch-name", default="")
    parser.add_argument("--branch-prefix", default="nexus-coding-smoke")
    parser.add_argument("--model", default=os.environ.get("NEXUS_CODING_SMOKE_MODEL", "coder"))
    parser.add_argument("--profile-id", default=os.environ.get("NEXUS_CODING_SMOKE_PROFILE_ID", "fixture_median"))
    parser.add_argument("--profile-label", default=os.environ.get("NEXUS_CODING_SMOKE_PROFILE_LABEL", "Fixture median repair"))
    parser.add_argument("--complexity", default=os.environ.get("NEXUS_CODING_SMOKE_COMPLEXITY", "simple"))
    parser.add_argument("--prompt", default="")
    parser.add_argument("--timeout-sec", type=float, default=float(os.environ.get("NEXUS_CODING_SMOKE_TIMEOUT_SEC", "1200")))
    parser.add_argument("--poll-sec", type=float, default=float(os.environ.get("NEXUS_CODING_SMOKE_POLL_SEC", "10")))
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
    parser.add_argument("--stalled-after-sec", type=float, default=180.0)
    parser.add_argument("--auto-intervene", action="store_true", default=os.environ.get("NEXUS_CODING_SMOKE_AUTO_INTERVENE", "true").lower() not in {"0", "false", "no"})
    parser.add_argument("--max-interventions", type=int, default=int(os.environ.get("NEXUS_CODING_SMOKE_MAX_INTERVENTIONS", "2")))
    parser.add_argument("--allowed-change", action="append", default=[])
    parser.add_argument("--output-dir", default=os.environ.get("NEXUS_CODING_SMOKE_OUTPUT_DIR", ""))
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    report: dict[str, Any] = {"schema": SCHEMA, "ok": False, "phases": []}
    try:
        report = run_smoke(args)
        return_code = 0
    except SmokeFailure as exc:
        if isinstance(getattr(exc, "report", None), dict):
            report = exc.report
        report["ok"] = False
        report["error"] = str(exc)
        report["finished_at"] = int(time.time())
        if report.get("started_at"):
            report["duration_sec"] = int(report["finished_at"] - int(report.get("started_at") or report["finished_at"]))
        return_code = 1
    except KeyboardInterrupt:
        report["ok"] = False
        report["error"] = "interrupted"
        report["finished_at"] = int(time.time())
        if report.get("started_at"):
            report["duration_sec"] = int(report["finished_at"] - int(report.get("started_at") or report["finished_at"]))
        return_code = 130
    finally:
        try:
            write_report(report, args.output_dir)
        except Exception as exc:
            report["report_write_error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
