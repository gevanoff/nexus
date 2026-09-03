#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

ALBATROSS_TESTED_VERSION = "2.4.0"
ALBATROSS_TESTED_COMMIT = "6f20178d81c6f0fdbb97ccf826b0d56f04a77faf"
FIXTURE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RESERVED_PARTS = {".git", ".albatross", ".small-harness"}
RESERVED_FILES = {"agent.config.json", ".env"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_rel_path(value: str) -> Path:
    path = Path(str(value or "").strip())
    if not str(path) or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
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
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "(redacted)")
    out = re.sub(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1(redacted)", out)
    out = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "(redacted)", out)
    out = re.sub(r"(?i)\bsk-(?:or-)?[A-Za-z0-9_-]{8,}\b", "(redacted)", out)
    return out


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "(redacted)" if SECRET_KEY_RE.search(str(key)) else redact_value(item, secrets)
            for key, item in value.items()
        }
    return value


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = read_json(path)
    if not isinstance(fixture, dict):
        raise ValueError("fixture must be a JSON object")
    if int(fixture.get("schema_version") or 0) != FIXTURE_SCHEMA_VERSION:
        raise ValueError(f"fixture schema_version must be {FIXTURE_SCHEMA_VERSION}")
    fixture_id = str(fixture.get("id") or "")
    if not SAFE_ID_RE.fullmatch(fixture_id):
        raise ValueError("fixture id contains unsafe characters")
    if not str(fixture.get("mission") or "").strip():
        raise ValueError(f"fixture {fixture_id} has no mission")
    repo = fixture.get("repository")
    files = repo.get("files") if isinstance(repo, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError(f"fixture {fixture_id} repository.files must be non-empty")
    repo["files"] = {safe_rel_path(str(key)).as_posix(): str(value) for key, value in files.items()}
    expected = fixture.setdefault("expected", {})
    if not isinstance(expected, dict):
        raise ValueError("expected must be an object")
    for key in ("files_changed", "allowed_files_changed"):
        if key in expected:
            if not isinstance(expected[key], list):
                raise ValueError(f"expected.{key} must be an array")
            expected[key] = [safe_rel_path(str(item)).as_posix() for item in expected[key]]
    for key in ("file_contains", "file_not_contains"):
        checks = expected.get(key) or []
        if not isinstance(checks, list):
            raise ValueError(f"expected.{key} must be an array")
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError(f"expected.{key} entries must be objects")
            check["path"] = safe_rel_path(str(check.get("path") or "")).as_posix()
            if not str(check.get("needle") or ""):
                raise ValueError(f"expected.{key} needle must be non-empty")
    for command in expected.get("validation") or []:
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError("validation commands must be non-empty argv arrays")
    limits = fixture.setdefault("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    limits["wall_time_sec"] = int(limits.get("wall_time_sec") or 300)
    limits["max_agent_steps"] = int(limits.get("max_agent_steps") or 20)
    if not 5 <= limits["wall_time_sec"] <= 3600:
        raise ValueError("wall_time_sec must be between 5 and 3600")
    if not 1 <= limits["max_agent_steps"] <= 100:
        raise ValueError("max_agent_steps must be between 1 and 100")
    return fixture


def run_process(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                timeout_sec: float = 60.0, secrets: Iterable[str] = ()) -> dict[str, Any]:
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, text=True, capture_output=True,
                              timeout=timeout_sec, check=False)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "timed_out": False,
            "stdout": redact_text(proc.stdout[-100000:], secrets),
            "stderr": redact_text(proc.stderr[-100000:], secrets),
            "duration_ms": round((time.monotonic() - start) * 1000.0, 1),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return {
            "ok": False,
            "returncode": None,
            "timed_out": True,
            "stdout": redact_text(stdout[-100000:], secrets),
            "stderr": redact_text(f"timeout after {timeout_sec}s\n{stderr[-100000:]}", secrets),
            "duration_ms": round((time.monotonic() - start) * 1000.0, 1),
        }


def git(argv: list[str], *, cwd: Path) -> dict[str, Any]:
    return run_process(["git", *argv], cwd=cwd)


def initialize_workspace(workspace: Path, fixture: dict[str, Any]) -> str:
    workspace.mkdir(parents=True, exist_ok=False)
    root = workspace.resolve()
    for rel, content in fixture["repository"]["files"].items():
        target = (workspace / rel).resolve()
        if root not in target.parents:
            raise ValueError(f"fixture path escapes workspace: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    for argv in (["init"], ["config", "user.email", "coding-harness@example.invalid"],
                 ["config", "user.name", "Coding Harness Eval"], ["add", "."],
                 ["commit", "-m", "fixture baseline", "--allow-empty"]):
        result = git(list(argv), cwd=workspace)
        if not result["ok"]:
            raise RuntimeError(f"git {' '.join(argv)} failed: {result['stderr'] or result['stdout']}")
    with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write("\n# Nexus harness runtime\n.albatross/\n.small-harness/\n")
    head = git(["rev-parse", "HEAD"], cwd=workspace)
    if not head["ok"]:
        raise RuntimeError("could not determine fixture baseline")
    return head["stdout"].strip()


def parse_status_files(text: str) -> list[str]:
    files: list[str] = []
    for line in str(text or "").splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            files.append(value)
    return sorted(set(files))


def workspace_snapshot(workspace: Path, baseline: str, artifacts: Path) -> dict[str, Any]:
    status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=workspace)
    head = git(["rev-parse", "HEAD"], cwd=workspace)
    committed_names = git(["diff", "--name-only", f"{baseline}..HEAD"], cwd=workspace)
    pieces = [
        git(["diff", "--binary", f"{baseline}..HEAD"], cwd=workspace)["stdout"].strip(),
        git(["diff", "--binary", "--cached"], cwd=workspace)["stdout"].strip(),
        git(["diff", "--binary"], cwd=workspace)["stdout"].strip(),
    ]
    diff_text = "\n".join(piece for piece in pieces if piece)
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "final.diff").write_text(diff_text + ("\n" if diff_text else ""), encoding="utf-8")
    committed = [line.strip() for line in committed_names["stdout"].splitlines() if line.strip()]
    changed = sorted(set(parse_status_files(status["stdout"])) | set(committed))
    final_files = artifacts / "final-files"
    for rel in changed:
        try:
            safe = safe_rel_path(rel)
            source = workspace / safe
            if not source.is_file() or source.stat().st_size > 2_000_000:
                continue
            target = final_files / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except (OSError, ValueError):
            continue
    return {
        "base_head": baseline,
        "final_head": head["stdout"].strip(),
        "dirty": bool(parse_status_files(status["stdout"])),
        "files_changed": changed,
        "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
        "diff_chars": len(diff_text),
    }


def build_albatross_env(*, nexus_base_url: str, nexus_token: str, model: str,
                        workspace: Path, home: Path, temp_dir: Path, max_steps: int) -> dict[str, str]:
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if os.environ.get(key)}
    env.update({
        "HOME": str(home),
        "TMPDIR": str(temp_dir),
        "BACKEND": "openai",
        "OPENAI_BASE_URL": nexus_base_url.rstrip("/"),
        "OPENAI_API_KEY": nexus_token,
        "AGENT_MODEL": model,
        "WORKSPACE_ROOT": str(workspace),
        "OUTSIDE_WORKSPACE": "deny",
        "ALBATROSS_NO_WIZARD": "true",
        "ALBATROSS_NO_UPDATE_CHECK": "true",
        "APPROVAL_POLICY": "always",
        "AGENT_MAX_STEPS": str(max_steps),
        "AGENT_TOOLS": "apply_patch,file_read,file_write,file_edit,glob,grep,list_dir,run_tests,shell,update_plan",
        "AGENT_TOOL_SELECTION": "fixed",
        "WARMUP": "false",
        "NO_COLOR": "1",
    })
    return env


def validation_env(home: Path, temp_dir: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if os.environ.get(key)}
    env.update({"HOME": str(home), "TMPDIR": str(temp_dir), "NO_COLOR": "1"})
    return env


def albatross_version(executable: str) -> dict[str, Any]:
    resolved = shutil.which(executable) if os.sep not in executable else executable
    if not resolved or not Path(resolved).exists():
        return {"installed": False, "executable": executable, "version": "", "raw": "albatross unavailable"}
    result = run_process([resolved, "--version"], cwd=Path.cwd(), timeout_sec=15)
    text = (result["stdout"] or result["stderr"]).strip()
    match = re.search(r"(?:albatross\s+)?v?(\d+\.\d+\.\d+)", text, re.I)
    return {"installed": bool(result["ok"]), "executable": resolved,
            "version": match.group(1) if match else "", "raw": text[:500]}


def parse_trace(run_root: Path) -> dict[str, Any]:
    tool_calls: list[str] = []
    turns: set[int] = set()
    steps = 0
    context_resets = 0
    malformed = 0
    trace_files: list[str] = []
    for path in sorted(run_root.rglob("*.events.jsonl")):
        trace_files.append(str(path.relative_to(run_root)))
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except Exception:
                malformed += 1
                continue
            turn = item.get("turn")
            if isinstance(turn, int):
                turns.add(turn)
            kind = str(item.get("kind") or "")
            if kind == "toolCall":
                tool_calls.append(str(item.get("name") or ""))
            elif kind == "contextCompacted":
                context_resets += 1
            elif kind == "turnSummary":
                steps += int(item.get("steps") or 0)
    return {
        "tool_calls": [name for name in tool_calls if name],
        "tool_call_count": len([name for name in tool_calls if name]),
        "agent_turns": len(turns),
        "agent_steps": steps,
        "context_resets": context_resets,
        "malformed_trace_lines": malformed,
        "trace_files": trace_files,
    }


def run_validation(fixture: dict[str, Any], workspace: Path, env: dict[str, str],
                   home: Path, temp_dir: Path) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    clean_env = validation_env(home, temp_dir)
    for argv in fixture.get("expected", {}).get("validation") or []:
        result = run_process([str(item) for item in argv], cwd=workspace, env=clean_env,
                             timeout_sec=min(300, fixture["limits"]["wall_time_sec"]))
        commands.append({"argv": argv, **result})
    return {"commands": commands, "passed": None if not commands else all(item["ok"] for item in commands)}


def objective_checks(fixture: dict[str, Any], workspace: Path, changed: list[str],
                     validation: dict[str, Any]) -> dict[str, Any]:
    expected = fixture.get("expected", {})
    checks: list[dict[str, Any]] = []
    exact = expected.get("files_changed")
    if exact is not None:
        wanted = sorted(str(item) for item in exact)
        checks.append({"kind": "files_changed", "passed": sorted(changed) == wanted,
                       "expected": wanted, "actual": sorted(changed)})
    allowed = expected.get("allowed_files_changed")
    if allowed is not None:
        allowed_set = {str(item) for item in allowed}
        checks.append({"kind": "allowed_files_changed", "passed": set(changed) <= allowed_set,
                       "allowed": sorted(allowed_set), "actual": sorted(changed)})
    for key, negate in (("file_contains", False), ("file_not_contains", True)):
        for spec in expected.get(key) or []:
            path = workspace / safe_rel_path(str(spec["path"]))
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            found = str(spec["needle"]) in text
            checks.append({"kind": key, "path": spec["path"], "needle": spec["needle"],
                           "passed": not found if negate else found})
    if validation.get("passed") is not None:
        checks.append({"kind": "validation", "passed": bool(validation["passed"])})
    return {"passed": None if not checks else all(item["passed"] for item in checks), "checks": checks}


def scrub_tree(root: Path, secrets: Iterable[str]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8")
            redacted = redact_text(text, secrets)
            if redacted != text:
                path.write_text(redacted, encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            pass


def mission_prompt(fixture: dict[str, Any], *, allow_mutations: bool) -> str:
    mode = "You may edit files and run tests." if allow_mutations else "This is read-only: do not mutate files or run mutating commands."
    return (
        f"{fixture['mission'].strip()}\n\n{mode} Work only inside the current repository workspace. "
        "Do not modify .git, .albatross, .small-harness, agent.config.json, or files outside the workspace. "
        "Do not commit or push. Use repository tools rather than guessing file contents."
    )


def run_albatross_fixture(fixture_path: Path, *, out_root: Path, executable: str,
                          nexus_base_url: str, nexus_token: str, model: str = "coder",
                          allow_mutations: bool = True) -> tuple[dict[str, Any], Path]:
    fixture = load_fixture(fixture_path)
    version = albatross_version(executable)
    if not version["installed"]:
        raise RuntimeError("albatross unavailable; install it separately before running this harness")
    if not nexus_token:
        raise RuntimeError("Nexus bearer token is required (NEXUS_API_KEY or GATEWAY_BEARER_TOKEN)")
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    run_root = out_root.resolve() / run_id / fixture["id"] / "albatross"
    workspace, artifacts = run_root / "workspace", run_root / "artifacts"
    home, temp_dir = run_root / "home", run_root / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    baseline = initialize_workspace(workspace, fixture)
    env = build_albatross_env(nexus_base_url=nexus_base_url, nexus_token=nexus_token, model=model,
                              workspace=workspace, home=home, temp_dir=temp_dir,
                              max_steps=fixture["limits"]["max_agent_steps"])
    argv = [version["executable"], "--print", mission_prompt(fixture, allow_mutations=allow_mutations)]
    if allow_mutations:
        argv.append("--allow-tools")
    started_at, start = now_iso(), time.monotonic()
    process = run_process(argv, cwd=workspace, env=env, timeout_sec=fixture["limits"]["wall_time_sec"],
                          secrets=[nexus_token])
    duration_ms, finished_at = round((time.monotonic() - start) * 1000.0, 1), now_iso()
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "stdout.txt").write_text(process["stdout"], encoding="utf-8")
    (artifacts / "stderr.txt").write_text(process["stderr"], encoding="utf-8")
    workspace_info = workspace_snapshot(workspace, baseline, artifacts)
    validation = run_validation(fixture, workspace, env, home, temp_dir)
    objective = objective_checks(fixture, workspace, workspace_info["files_changed"], validation)
    scrub_tree(run_root, [nexus_token])
    trace = parse_trace(run_root)
    result = redact_value({
        "schema_version": RESULT_SCHEMA_VERSION,
        "fixture_id": fixture["id"],
        "fixture_description": fixture.get("description", ""),
        "tags": fixture.get("tags", []),
        "harness": "albatross",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "harness_version": {"version": version["version"], "tested_version": ALBATROSS_TESTED_VERSION,
                            "tested_commit": ALBATROSS_TESTED_COMMIT},
        "model": {"requested": model, "gateway": "nexus", "nexus_base_url": nexus_base_url,
                  "client_backend": "openai", "backend": "", "upstream_model": "",
                  "route_evidence": "not_available_from_albatross_adapter_v1"},
        "outcome": {"status": "completed" if process["ok"] and objective["passed"] is not False
                    else ("timed_out" if process["timed_out"] else "failed"),
                    "completed": process["ok"] and objective["passed"] is not False,
                    "interrupted": process["timed_out"], "exit_code": process["returncode"],
                    "error": None if process["ok"] else (process["stderr"] or f"exit {process['returncode']}")},
        "workspace": workspace_info,
        "validation": validation,
        "trajectory": {"agent_turns": trace["agent_turns"], "agent_steps": trace["agent_steps"],
                       "tool_calls": trace["tool_call_count"], "tool_call_names": trace["tool_calls"],
                       "context_resets": trace["context_resets"],
                       "malformed_trace_lines": trace["malformed_trace_lines"]},
        "objective": objective,
        "artifacts": {"run_root": str(run_root), "stdout": str(artifacts / "stdout.txt"),
                      "stderr": str(artifacts / "stderr.txt"), "diff": str(artifacts / "final.diff"),
                      "trace_files": trace["trace_files"]},
    }, [nexus_token])
    result_path = artifacts / "result.json"
    write_json(result_path, result)
    return result, result_path


def probe(executable: str, *, live: bool = False, out_root: Path | None = None,
          nexus_base_url: str = "http://ai2:8800/v1", nexus_token: str = "",
          model: str = "coder") -> dict[str, Any]:
    version = albatross_version(executable)
    report: dict[str, Any] = {
        "ok": version["installed"],
        "albatross": {**version, "tested_version": ALBATROSS_TESTED_VERSION,
                      "tested_commit": ALBATROSS_TESTED_COMMIT},
        "nexus": {"base_url": nexus_base_url, "model": model},
        "capabilities": {},
    }
    if not version["installed"]:
        return report
    help_result = run_process([version["executable"], "--help"], cwd=Path.cwd(), timeout_sec=15)
    help_text = help_result["stdout"] + "\n" + help_result["stderr"]
    report["capabilities"] = {
        "one_shot": "--print" in help_text,
        "external_eval": "--eval" in help_text,
        "json_eval_output": "--json" in help_text,
        "allow_tools": "--allow-tools" in help_text,
        "chat": None,
        "streaming": None,
        "tool_calls": None,
        "structured_trace": None,
    }
    if not live:
        return report
    if not nexus_token:
        report["ok"] = False
        report["live_error"] = "Nexus bearer token is required for --live"
        return report
    root = (out_root or Path(".runtime/coding-harness-evals")).resolve()
    probe_fixture = root / "probe" / "read-only-probe.json"
    write_json(probe_fixture, {
        "schema_version": 1,
        "id": "read-only-probe",
        "description": "Read-only Albatross through Nexus capability probe.",
        "repository": {"files": {"probe.txt": "NEXUS_ALBATROSS_PROBE_OK\n"}},
        "mission": "Use a repository read tool to read probe.txt and report the exact token NEXUS_ALBATROSS_PROBE_OK. Do not edit files.",
        "expected": {"file_contains": [{"path": "probe.txt", "needle": "NEXUS_ALBATROSS_PROBE_OK"}]},
        "limits": {"wall_time_sec": 120, "max_agent_steps": 8},
        "tags": ["probe", "read-only"],
    })
    try:
        result, result_path = run_albatross_fixture(
            probe_fixture, out_root=root, executable=executable, nexus_base_url=nexus_base_url,
            nexus_token=nexus_token, model=model, allow_mutations=False)
    except Exception as exc:
        report["ok"] = False
        report["live_error"] = f"{type(exc).__name__}: {exc}"
        return redact_value(report, [nexus_token])
    tools = result.get("trajectory", {}).get("tool_call_names") or []
    chat_ok = result.get("outcome", {}).get("exit_code") == 0
    report["capabilities"].update({
        "chat": chat_ok,
        "streaming": chat_ok,
        "tool_calls": "file_read" in tools,
        "structured_trace": bool(result.get("artifacts", {}).get("trace_files")),
    })
    report["live_result"] = str(result_path)
    report["ok"] = bool(chat_ok and report["capabilities"]["tool_calls"])
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
    lines += ["  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in rows]
    return "\n".join(lines)


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
        values = [{"path": str(path), "id": load_fixture(path)["id"],
                   "description": load_fixture(path).get("description", "")} for path in bundled_fixture_paths()]
        if args.json:
            print(json.dumps(values, indent=2))
        else:
            for item in values:
                print(f"{item['id']}: {item['description']} ({item['path']})")
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
        results = [read_json(Path(path)) for path in args.results]
        print(json.dumps(results, indent=2, sort_keys=True) if args.json else render_comparison(results))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
