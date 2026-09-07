from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from app.config import S, logger
from app import coding_model_policy
from app import model_integration_workspace as miw
from app.coding_runtime_guardrails import (
    archive_stop_diagnostics,
    archive_stop_finding,
    redacted_archive_run,
)


SCHEMA = "nexus_coding_task.v1"
_SAFE_TASK_RE = re.compile(r"^code_[a-f0-9]{12}$")
_SAFE_ARCHIVE_RE = re.compile(r"^code_[a-f0-9]{12}\.\d+\.[a-f0-9]+$")
_SAFE_REF_RE = re.compile(r"[^A-Za-z0-9._/-]+")
_BLOCKED_GIT_SUBCOMMANDS = {
    "clean",
    "filter-branch",
    "gc",
    "merge",
    "rebase",
    "reset",
    "restore",
    "rm",
    "submodule",
    "worktree",
}
_TREE_SKIP = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
_ARCHIVE_ANALYSIS_MODES = {"manual", "idle", "immediate"}
_ARCHIVE_ANALYSIS_TARGETS = {"local", "external", "human", "none"}
_FINDING_REVIEW_VERDICTS = {"invalid", "superseded"}
_PROJECT_PLAN_STATUSES = {"pending", "in_progress", "completed", "blocked", "skipped"}
_HARNESS_FIXTURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_HARNESS_RESERVED_PATH_PARTS = {".git", ".nexus"}
_HARNESS_MAX_FILES = 4096
_HARNESS_MAX_FILE_BYTES = 2_000_000
_HARNESS_MAX_TOTAL_BYTES = 8_000_000
_HARNESS_MAX_PROMPT_BYTES = 64_000
_HARNESS_MAX_CHANGED_FILES = 512
_HARNESS_MAX_DIFF_CHARS = 8_000_000
_HARNESS_VALIDATION_FILE_BYTES = 1_024 * 1_024
_HARNESS_VALIDATION_OPEN_FILES = 64
_HARNESS_VALIDATION_PROCESSES = 128
_HARNESS_VALIDATION_MEMORY_BYTES = 16 * 1_024 * 1_024 * 1_024
_HARNESS_VALIDATION_AGGREGATE_MEMORY_BYTES = 2 * 1_024 * 1_024 * 1_024
_HARNESS_VALIDATION_SCRATCH_BYTES = 64 * 1_024 * 1_024
_HARNESS_VALIDATION_SCRATCH_ENTRIES = 4_096
_HARNESS_VALIDATION_STAGE_BYTES = 128 * 1_024 * 1_024
_HARNESS_VALIDATION_STAGE_ENTRIES = 16_384
_JSON_LOCKS_GUARD = threading.Lock()
_JSON_LOCKS: Dict[str, threading.RLock] = {}
_DELETED_TASKS_GUARD = threading.Lock()
_DELETED_TASK_TOMBSTONES: set[str] = set()
_WORKSPACE_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: Dict[str, threading.RLock] = {}
_HARNESS_VALIDATIONS_GUARD = threading.Lock()
_ACTIVE_HARNESS_VALIDATIONS: set[str] = set()
_ACTIVE_HARNESS_AGENT_TOOLS: Dict[str, int] = {}
_ACTIVE_HARNESS_EVIDENCE_READS: Dict[str, int] = {}
_ACTIVE_HARNESS_RUN_STARTS: set[str] = set()
_ACTIVE_HARNESS_EVIDENCE_LEASES: Dict[str, Dict[str, Any]] = {}
_HARNESS_EVIDENCE_LEASE_MAX_SEC = 7200.0


def coding_enabled() -> bool:
    return bool(getattr(S, "CODING_ENABLED", True))


def workspace_root() -> Path:
    return Path(getattr(S, "CODING_WORKSPACE_ROOT", "") or "/var/lib/gateway/data/coding/workspaces").resolve()


def tasks_dir() -> Path:
    return Path(getattr(S, "CODING_TASKS_DIR", "") or "/var/lib/gateway/data/coding/tasks").resolve()


def command_timeout_sec(value: Optional[float] = None) -> float:
    default = float(getattr(S, "CODING_COMMAND_TIMEOUT_SEC", 120) or 120)
    if value is None:
        return max(1.0, min(default, 3600.0))
    try:
        requested = float(value)
    except Exception:
        requested = default
    return max(1.0, min(requested, default, 3600.0))


def max_output_chars() -> int:
    try:
        return max(1_000, min(2_000_000, int(getattr(S, "CODING_MAX_OUTPUT_CHARS", 40_000) or 40_000)))
    except Exception:
        return 40_000


def file_max_bytes() -> int:
    try:
        return max(1_000, min(20_000_000, int(getattr(S, "CODING_FILE_MAX_BYTES", 500_000) or 500_000)))
    except Exception:
        return 500_000


def allowed_commands() -> List[str]:
    raw = str(getattr(S, "CODING_ALLOWED_COMMANDS", "") or "")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    seen = set()
    out: List[str] = []
    for item in values:
        name = Path(item).name.lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _now() -> float:
    return time.time()


def _ensure_enabled() -> None:
    if not coding_enabled():
        raise HTTPException(status_code=403, detail="coding workspaces are disabled")


def _ensure_dirs() -> None:
    workspace_root().mkdir(parents=True, exist_ok=True)
    tasks_dir().mkdir(parents=True, exist_ok=True)


def _task_path(task_id: str) -> Path:
    task_id = str(task_id or "").strip()
    if not _SAFE_TASK_RE.match(task_id):
        raise HTTPException(status_code=404, detail="coding task not found")
    return tasks_dir().joinpath(f"{task_id}.json").resolve()


def _task_tombstone_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def _mark_task_deleted(path: Path) -> None:
    with _DELETED_TASKS_GUARD:
        _DELETED_TASK_TOMBSTONES.add(_task_tombstone_key(path))


def _raise_if_task_deleted(path: Path) -> None:
    with _DELETED_TASKS_GUARD:
        deleted = _task_tombstone_key(path) in _DELETED_TASK_TOMBSTONES
    if deleted:
        raise HTTPException(status_code=409, detail="coding task has been deleted")


def _corrupt_tasks_dir() -> Path:
    return tasks_dir().joinpath("_corrupt").resolve()


def _archived_tasks_dir() -> Path:
    return tasks_dir().joinpath("_archive").resolve()


def _archived_workspaces_dir() -> Path:
    return workspace_root().joinpath("_archive").resolve()


def _legacy_archived_tasks_dir() -> Path:
    return tasks_dir().parent.joinpath("archived", "tasks").resolve()


def _legacy_archived_workspaces_dir() -> Path:
    return workspace_root().parent.joinpath("archived", "workspaces").resolve()


def _archive_retention_sec() -> int:
    try:
        return max(3600, int(getattr(S, "CODING_ARCHIVE_RETENTION_SEC", 7 * 24 * 3600) or (7 * 24 * 3600)))
    except Exception:
        return 7 * 24 * 3600


def _default_archive_delete_after(archived_at: float) -> int:
    return int(max(0.0, float(archived_at or 0)) + float(_archive_retention_sec()))


def _archive_task_roots() -> List[Path]:
    roots = [_archived_tasks_dir(), _legacy_archived_tasks_dir()]
    out: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def _archive_workspace_roots() -> List[Path]:
    roots = [_archived_workspaces_dir(), _legacy_archived_workspaces_dir()]
    out: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def _validate_archive_id(archive_id: str) -> str:
    value = str(archive_id or "").strip()
    if not _SAFE_ARCHIVE_RE.match(value):
        raise HTTPException(status_code=404, detail="archived coding task not found")
    return value


def _archive_manifest_path(archive_id: str) -> Path:
    archive_id = _validate_archive_id(archive_id)
    for root in _archive_task_roots():
        path = root.joinpath(f"{archive_id}.manifest.json")
        if path.exists():
            return path.resolve()
    return _archived_tasks_dir().joinpath(f"{archive_id}.manifest.json").resolve()


def _archive_task_json_path(archive_id: str) -> Path:
    archive_id = _validate_archive_id(archive_id)
    for root in _archive_task_roots():
        path = root.joinpath(f"{archive_id}.json")
        if path.exists():
            return path.resolve()
    return _archived_tasks_dir().joinpath(f"{archive_id}.json").resolve()


def _archive_findings_path(archive_id: str) -> Path:
    manifest_path = _archive_manifest_path(archive_id)
    return manifest_path.with_name(f"{archive_id}.findings.jsonl").resolve()


def _archive_external_brief_path(archive_id: str) -> Path:
    manifest_path = _archive_manifest_path(archive_id)
    return manifest_path.with_name(f"{archive_id}.external-agent.md").resolve()


def _archive_workspace_path(archive_id: str) -> Path:
    archive_id = _validate_archive_id(archive_id)
    for root in _archive_workspace_roots():
        path = root.joinpath(archive_id)
        if path.exists():
            return path.resolve()
    return _archived_workspaces_dir().joinpath(archive_id).resolve()


def _read_findings_log(path: Path, *, limit: int = 20) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for raw in lines[-max(1, limit) :]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _apply_finding_reviews(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    reviews: Dict[int, Dict[str, Any]] = {}
    visible: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") == "sentinel_archive_review":
            try:
                target_ts = int(float(item.get("reviewed_finding_ts") or 0))
            except Exception:
                target_ts = 0
            if target_ts > 0:
                previous = reviews.get(target_ts)
                try:
                    item_ts = int(float(item.get("ts") or 0))
                except Exception:
                    item_ts = 0
                try:
                    previous_ts = int(float((previous or {}).get("ts") or 0))
                except Exception:
                    previous_ts = 0
                if previous is None or item_ts >= previous_ts:
                    reviews[target_ts] = {
                        "verdict": str(item.get("review_verdict") or "").strip(),
                        "note": str(item.get("note") or "").strip(),
                        "actor": str(item.get("actor") or "").strip(),
                        "ts": item.get("ts"),
                    }
            continue
        visible.append(dict(item))
    for item in visible:
        try:
            finding_ts = int(float(item.get("ts") or 0))
        except Exception:
            finding_ts = 0
        if finding_ts > 0 and finding_ts in reviews:
            item["review"] = dict(reviews[finding_ts])
    return visible


def _archive_prompt_mentions_node(prompt: str) -> bool:
    lowered = str(prompt or "").lower()
    return any(token in lowered for token in ("package.json", "npm", "node", "javascript", "typescript", "frontend", "webapp"))


def _archive_default_analysis(task: Dict[str, Any], *, archived_at: float, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    analysis = dict(analysis or {})
    requested_mode = str(analysis.get("requested_mode") or "idle").strip().lower()
    if requested_mode not in _ARCHIVE_ANALYSIS_MODES:
        requested_mode = "idle"
    target = str(analysis.get("target") or "local").strip().lower()
    if target not in _ARCHIVE_ANALYSIS_TARGETS:
        target = "local"
    local_model = str(analysis.get("local_model") or task.get("coding_model") or "coder").strip() or "coder"
    status = str(analysis.get("status") or "").strip().lower()
    if not status:
        if target == "none":
            status = "disabled"
        elif target == "human" or requested_mode == "manual":
            status = "manual"
        elif target == "external":
            status = "external_pending"
        else:
            status = "pending"
    return {
        "requested_mode": requested_mode,
        "target": target,
        "local_model": local_model if target == "local" else "",
        "status": status,
        "last_requested_at": float(analysis.get("last_requested_at") or archived_at or _now()),
        "last_started_at": float(analysis.get("last_started_at") or 0),
        "last_finished_at": float(analysis.get("last_finished_at") or 0),
        "last_summary": str(analysis.get("last_summary") or "")[:2000],
        "last_error": str(analysis.get("last_error") or "")[:4000],
        "findings_count": int(analysis.get("findings_count") or 0),
    }


def _archive_default_retention(*, archived_at: float, retention: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    retention = dict(retention or {})
    preserve = bool(retention.get("preserve"))
    delete_after_ts = retention.get("delete_after_ts")
    try:
        delete_after_int = int(delete_after_ts or 0)
    except Exception:
        delete_after_int = 0
    if preserve:
        delete_after_int = 0
    elif delete_after_int <= 0:
        delete_after_int = _default_archive_delete_after(archived_at)
    return {
        "preserve": preserve,
        "delete_after_ts": delete_after_int,
        "retention_sec": int(retention.get("retention_sec") or _archive_retention_sec()),
    }


def _load_archived_task_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_archive_manifest(manifest: Dict[str, Any], *, manifest_path: Path, task: Dict[str, Any]) -> Dict[str, Any]:
    archive_id = _validate_archive_id(str(manifest.get("archive_id") or manifest_path.name[: -len(".manifest.json")]))
    archived_at = float(manifest.get("archived_at") or _now())
    findings_path = str(manifest.get("findings_path") or manifest_path.with_name(f"{archive_id}.findings.jsonl"))
    normalized = dict(manifest)
    normalized.update(
        {
            "archive_id": archive_id,
            "task_id": str(manifest.get("task_id") or task.get("id") or archive_id.split(".", 1)[0]),
            "archived_at": archived_at,
            "actor": str(manifest.get("actor") or "").strip(),
            "reason": str(manifest.get("reason") or "manual_archive").strip() or "manual_archive",
            "repo_url": redact_repo_url(str(manifest.get("repo_url") or task.get("repo_url") or "")),
            "status": str(manifest.get("status") or task.get("status") or ""),
            "agent_status": str(manifest.get("agent_status") or task.get("agent_status") or ""),
            "task_path": str(manifest.get("task_path") or _archive_task_json_path(archive_id)),
            "workspace_path": str(manifest.get("workspace_path") or _archive_workspace_path(archive_id)),
            "findings_path": findings_path,
            "external_brief_path": str(manifest.get("external_brief_path") or _archive_external_brief_path(archive_id)),
            "manifest_path": str(manifest_path),
            "analysis": _archive_default_analysis(task, archived_at=archived_at, analysis=manifest.get("analysis") if isinstance(manifest.get("analysis"), dict) else None),
            "retention": _archive_default_retention(archived_at=archived_at, retention=manifest.get("retention") if isinstance(manifest.get("retention"), dict) else None),
        }
    )
    return normalized


def _write_archive_external_brief(archive_id: str, manifest: Dict[str, Any], task: Dict[str, Any]) -> str:
    diff_snapshot = _archive_diff_snapshot(task, manifest, max_diff_chars=8000)
    heuristics = _archive_heuristic_findings(task, manifest, diff_snapshot)
    path = Path(str(manifest.get("external_brief_path") or _archive_external_brief_path(archive_id))).resolve()
    lines = [
        f"# External Agent Follow-up: {archive_id}",
        "",
        "Use this archive as read-only forensic input. Inspect the archived workspace path directly; do not assume the current live repository matches it.",
        "",
        f"- Archive id: {archive_id}",
        f"- Task id: {task.get('id') or manifest.get('task_id') or ''}",
        f"- Workspace path: {manifest.get('workspace_path') or ''}",
        f"- Findings log: {manifest.get('findings_path') or ''}",
        f"- Original prompt: {str(task.get('prompt') or '').strip()}",
        f"- Base branch: {str(task.get('base_branch') or 'main').strip()}",
        "",
        "## Known Findings",
    ]
    if heuristics:
        for item in heuristics[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {str(item.get('summary') or '').strip()}")
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            for entry in evidence[:4]:
                lines.append(f"  - Evidence: {entry}")
    else:
        lines.append("- No static findings were extracted automatically.")
    lines.extend(
        [
            "",
            "## Diff Stat",
            "```text",
            str(diff_snapshot.get("stat") or "")[:4000],
            "```",
            "",
            "## Instructions",
            "1. Open the archived workspace path above, not the live repo.",
            "2. Verify the diff against the recorded base branch.",
            "3. Produce a concrete repair plan with exact files, code changes, and validation steps.",
            "4. Escalate to a stronger coding/review agent if the fix requires broader repository knowledge or cross-file reasoning beyond the archived diff.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return str(path)


def _archived_repo_path(manifest: Dict[str, Any], task: Dict[str, Any]) -> Path:
    repo_hint = Path(str(task.get("repo_path") or "repo")).name or "repo"
    candidates: List[Path] = []
    raw_workspace = str(manifest.get("workspace_path") or "").strip()
    if raw_workspace:
        candidates.append(Path(raw_workspace).resolve())
    archive_id = str(manifest.get("archive_id") or "").strip()
    if archive_id:
        candidates.append(_archive_workspace_path(archive_id))

    seen: set[str] = set()
    for workspace in candidates:
        key = str(workspace)
        if key in seen:
            continue
        seen.add(key)
        for candidate in (workspace.joinpath(repo_hint), workspace.joinpath("repo"), workspace):
            if candidate.exists():
                return candidate.resolve()

    fallback_workspace = candidates[-1] if candidates else _archive_workspace_path(archive_id)
    return fallback_workspace.joinpath(repo_hint).resolve()


def _archive_diff_snapshot(task: Dict[str, Any], manifest: Dict[str, Any], *, max_diff_chars: int = 12000) -> Dict[str, Any]:
    repo = _archived_repo_path(manifest, task)
    if not repo.exists() or not repo.is_dir():
        return {"ok": False, "repo_path": str(repo), "error": "archived repo is missing", "stat": "", "diff": "", "files": []}
    if not repo.joinpath(".git").exists():
        return {"ok": False, "repo_path": str(repo), "error": "archived repo does not contain .git metadata", "stat": "", "diff": "", "files": []}
    base = _git_base_branch_diff(repo, base_branch=str(task.get("base_branch") or "main"))
    workspace_diff = str(((base.get("diff") or {}).get("stdout") or ""))
    committed_diff = str(((base.get("committed_diff") or {}).get("stdout") or ""))
    workspace_files = (base.get("changes") or {}).get("files") if isinstance(base.get("changes"), dict) else []
    committed_files = (base.get("committed_changes") or {}).get("files") if isinstance(base.get("committed_changes"), dict) else []
    use_committed = not workspace_diff.strip() and bool(committed_diff.strip() or committed_files)
    diff_stdout = committed_diff if use_committed else workspace_diff
    if len(diff_stdout) > max_diff_chars:
        diff_stdout = diff_stdout[:max_diff_chars]
    stat_payload = (base.get("committed_stat") or {}) if use_committed else (base.get("stat") or {})
    files = committed_files if use_committed else workspace_files
    return {
        "ok": bool(base.get("diff", {}).get("ok", False) or base.get("changes")),
        "repo_path": str(repo),
        "base_branch": str(task.get("base_branch") or "main"),
        "scope": "committed" if use_committed else "workspace",
        "stat": str((stat_payload.get("stdout") or "")),
        "diff": diff_stdout,
        "files": files if isinstance(files, list) else [],
        "error": str(base.get("error") or ((base.get("diff") or {}).get("stderr") or "")),
    }


def _archive_heuristic_findings(task: Dict[str, Any], manifest: Dict[str, Any], diff_snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    diff_text = str(diff_snapshot.get("diff") or "")
    added_lines = [line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    lowered_prompt = str(task.get("prompt") or "")
    commands = task.get("commands") if isinstance(task.get("commands"), list) else []
    files = diff_snapshot.get("files") if isinstance(diff_snapshot.get("files"), list) else []
    added_paths = {str(item.get("path") or "") for item in files if isinstance(item, dict) and str(item.get("status") or "").upper() == "A"}
    diagnostics = archive_stop_diagnostics(task, manifest, redact=_redact_text)
    findings: List[Dict[str, Any]] = [archive_stop_finding(diagnostics)]

    placeholder_lines = [line.strip() for line in added_lines if any(marker in line.lower() for marker in ("add logic to", "placeholder", "todo", "stub", "no tests yet"))]
    if placeholder_lines:
        findings.append(
            {
                "severity": "warn",
                "code": "placeholder_fix",
                "summary": "The archived diff added placeholder or stub text instead of a concrete implementation.",
                "evidence": placeholder_lines[:5],
            }
        )

    if "services/gateway/package.json" in added_paths and not _archive_prompt_mentions_node(lowered_prompt):
        npm_commands = []
        for item in commands:
            if not isinstance(item, dict):
                continue
            argv = item.get("argv") if isinstance(item.get("argv"), list) else []
            argv_text = " ".join(str(part) for part in argv)
            if argv and str(argv[0]).lower() == "npm":
                npm_commands.append(argv_text)
        findings.append(
            {
                "severity": "error",
                "code": "invented_node_manifest",
                "summary": "The archived workspace introduced services/gateway/package.json for a task that did not ask for Node scaffolding.",
                "evidence": npm_commands[:5] or ["services/gateway/package.json was added in the workspace diff."],
            }
        )

    repo = _archived_repo_path(manifest, task)
    if "edit button" in str(task.get("prompt") or "").lower():
        tasks_js = repo.joinpath("services", "gateway", "app", "static", "tasks.js")
        tasks_html = repo.joinpath("services", "gateway", "app", "static", "tasks.html")
        try:
            js_text = tasks_js.read_text(encoding="utf-8") if tasks_js.exists() else ""
        except Exception:
            js_text = ""
        try:
            html_text = tasks_html.read_text(encoding="utf-8") if tasks_html.exists() else ""
        except Exception:
            html_text = ""
        if 'id="editTask"' in html_text and 'document.getElementById("editTask")' not in js_text:
            findings.append(
                {
                    "severity": "warn",
                    "code": "unwired_edit_button",
                    "summary": "The underlying bug was that tasks.html exposed editTask but tasks.js did not wire it into the UI state or event handlers.",
                    "evidence": ['tasks.html contains id="editTask" while tasks.js lacks document.getElementById("editTask")'],
                }
            )

    if not findings:
        findings.append(
            {
                "severity": "info",
                "code": "no_obvious_static_signature",
                "summary": "No high-confidence static anti-pattern was detected from the archived diff snapshot.",
                "evidence": [],
            }
        )
    return findings


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with _json_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def _json_lock(path: Path) -> threading.RLock:
    try:
        key = str(path.resolve())
    except Exception:
        key = str(path)
    with _JSON_LOCKS_GUARD:
        lock = _JSON_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JSON_LOCKS[key] = lock
        return lock


def task_workspace_lock(task_id: str) -> threading.RLock:
    """Serialize same-task worktree mutations and publish/finalization side effects."""
    value = str(task_id or "").strip()
    if not _SAFE_TASK_RE.match(value):
        raise HTTPException(status_code=404, detail="coding task not found")
    with _WORKSPACE_LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.get(value)
        if lock is None:
            lock = threading.RLock()
            _WORKSPACE_LOCKS[value] = lock
        return lock


def _quarantine_task_file(path: Path, raw_bytes: bytes, *, error_text: str) -> str:
    dest_dir = _corrupt_tasks_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix or ".json"
    dest = dest_dir.joinpath(f"{path.stem}.{int(_now())}.{secrets.token_hex(4)}{suffix}")
    dest.write_bytes(raw_bytes)
    dest.with_name(f"{dest.name}.error.txt").write_text(error_text, encoding="utf-8")
    return str(dest)


def _metadata_error_placeholder(path: Path, *, error_text: str, quarantined_path: str) -> Dict[str, Any]:
    task_id = path.stem if _SAFE_TASK_RE.match(path.stem) else ""
    now = _now()
    summary = (
        "Coding task metadata was unreadable and the original file was moved aside for inspection. "
        "This workspace must be recreated or repaired before another agent run can proceed."
    )
    repo_path = str(_repo_path_for(task_id)) if task_id else ""
    workspace_path = str(_task_workspace(task_id)) if task_id else ""
    return {
        "schema": SCHEMA,
        "id": task_id or path.stem,
        "kind": "workspace",
        "status": "error",
        "created_at": now,
        "updated_at": now,
        "owner": "",
        "repo_url": "",
        "base_branch": "main",
        "branch_name": "",
        "prompt": "",
        "workspace_path": workspace_path,
        "repo_path": repo_path,
        "commands": [],
        "guidance_messages": [],
        "agent_status": "failed",
        "agent_cycle": 0,
        "agent_last_event_at": now,
        "agent_summary": "",
        "agent_error": summary,
        "agent_events": [
            {
                "ts": now,
                "type": "failed",
                "error": error_text,
                "summary": summary,
            }
        ],
        "metadata_error": {
            "message": summary,
            "detail": error_text,
            "task_file": str(path),
            "quarantined_path": quarantined_path,
        },
    }


def _repair_unreadable_task_file(path: Path, *, raw_bytes: bytes, error_text: str) -> Dict[str, Any]:
    quarantined_path = _quarantine_task_file(path, raw_bytes, error_text=error_text)
    placeholder = _metadata_error_placeholder(path, error_text=error_text, quarantined_path=quarantined_path)
    _write_json(path, placeholder)
    logger.warning(
        "coding task metadata repaired path=%s quarantine=%s error=%s",
        path,
        quarantined_path,
        error_text,
    )
    return placeholder


def _read_json(path: Path) -> Dict[str, Any]:
    with _json_lock(path):
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="coding task not found")
        except Exception as exc:
            logger.warning("coding task read failed path=%s error=%s", path, exc)
            raise HTTPException(status_code=500, detail="coding task metadata is unreadable")
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _repair_unreadable_task_file(path, raw_bytes=raw_bytes, error_text=f"{type(exc).__name__}: {exc}")
        try:
            data = json.loads(text)
        except Exception as exc:
            return _repair_unreadable_task_file(path, raw_bytes=raw_bytes, error_text=f"{type(exc).__name__}: {exc}")
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return _repair_unreadable_task_file(path, raw_bytes=raw_bytes, error_text="task metadata schema is invalid")
        return data


def load_task(task_id: str) -> Dict[str, Any]:
    _ensure_enabled()
    task = _read_json(_task_path(task_id))
    mission = normalize_coding_mission(task)
    if task.get("mission") != mission:
        task["mission"] = mission
        save_task(task)
    return task


_DIRECT_CHANGE_RE = re.compile(
    r"\b(fix|repair|resolve|implement|edit|modify|patch|add|remove|create|rewrite|change|update)\b",
    re.IGNORECASE,
)
_REVIEW_GOAL_MARKERS = (
    "review this workspace",
    "review scope",
    "review only",
    "audit",
    "concrete findings",
    "behavioral regressions",
    "risky assumptions",
    "missing tests",
    "inspect relevant diffs",
)


def goal_expects_file_changes(goal: str) -> bool:
    text = " ".join(str(goal or "").strip().lower().split())
    if not text:
        return True
    review_goal = any(marker in text for marker in _REVIEW_GOAL_MARKERS)
    return bool(_DIRECT_CHANGE_RE.search(text)) or not review_goal


def normalize_coding_mission(task: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = task.get("mission") if isinstance(task.get("mission"), dict) else {}
    supplied = overrides if isinstance(overrides, dict) else {}
    completion = raw.get("completion_policy") if isinstance(raw.get("completion_policy"), dict) else {}
    completion.update(supplied.get("completion_policy") if isinstance(supplied.get("completion_policy"), dict) else {})
    publish = raw.get("publish_policy") if isinstance(raw.get("publish_policy"), dict) else {}
    publish.update(supplied.get("publish_policy") if isinstance(supplied.get("publish_policy"), dict) else {})
    budget = raw.get("budget_policy") if isinstance(raw.get("budget_policy"), dict) else {}
    budget.update(supplied.get("budget_policy") if isinstance(supplied.get("budget_policy"), dict) else {})
    context = raw.get("context_policy") if isinstance(raw.get("context_policy"), dict) else {}
    context.update(supplied.get("context_policy") if isinstance(supplied.get("context_policy"), dict) else {})
    prompt = str(supplied.get("goal") or raw.get("goal") or task.get("prompt") or "").strip()
    max_no_progress_cycles = int(budget.get("max_no_progress_cycles") or 8)
    expects_file_changes = goal_expects_file_changes(prompt)

    def completion_bool(name: str, default: bool) -> bool:
        return bool(completion[name]) if name in completion else default

    return {
        "schema": "nexus_coding_mission.v1",
        "goal": prompt,
        "repo_url": str(task.get("repo_url") or ""),
        "base_branch": str(task.get("base_branch") or "main"),
        "branch_name": str(task.get("branch_name") or ""),
        "completion_policy": {
            "require_file_changes": completion_bool("require_file_changes", expects_file_changes),
            "require_validation_after_edit": completion_bool("require_validation_after_edit", True),
            "require_diff_review_after_edit": completion_bool("require_diff_review_after_edit", True),
            "require_commit_on_success": completion_bool("require_commit_on_success", expects_file_changes),
            "commit_policy": str(completion.get("commit_policy") or "always_on_success"),
        },
        "publish_policy": {
            "push": str(publish.get("push") or "never"),
            "draft_pr": str(publish.get("draft_pr") or "never"),
            "remote": str(publish.get("remote") or "origin"),
            "pr_title": str(publish.get("pr_title") or ""),
            "pr_body": str(publish.get("pr_body") or ""),
        },
        "budget_policy": {
            "max_cycles": int(budget.get("max_cycles") or 1000),
            "max_runtime_sec": int(budget.get("max_runtime_sec") or 21600),
            "max_no_progress_cycles": max_no_progress_cycles,
            "recovery_checkpoint_cycles": int(
                budget.get("recovery_checkpoint_cycles")
                or min(8, max_no_progress_cycles)
            ),
            "long_model_max_no_progress_cycles": int(
                budget.get("long_model_max_no_progress_cycles") or 12
            ),
            "max_repeated_state_reads": int(budget.get("max_repeated_state_reads") or 6),
            "max_repeated_same_file_reads": int(budget.get("max_repeated_same_file_reads") or 4),
        },
        "context_policy": {
            "context_reset_cycles": int(context.get("context_reset_cycles") or 0),
            "context_reset_chars": int(context.get("context_reset_chars") or 64_000),
            "state_snapshot_on_reset": bool(context.get("state_snapshot_on_reset", True)),
        },
    }


def coding_mission_overrides(
    *,
    commit_policy: str = "always_on_success",
    push_on_success: bool = False,
    draft_pr_on_success: bool = False,
    pr_title: str = "",
    pr_body: str = "",
    max_cycles: Optional[int] = None,
    max_runtime_sec: Optional[int] = None,
    context_reset_cycles: Optional[int] = None,
) -> Dict[str, Any]:
    push = bool(push_on_success or draft_pr_on_success)
    completion_policy: Dict[str, Any] = {
        "commit_policy": str(commit_policy or "always_on_success"),
    }
    if push:
        completion_policy.update({
            "require_file_changes": True,
            "require_commit_on_success": True,
        })
    return {
        "completion_policy": completion_policy,
        "publish_policy": {
            "push": "on_success" if push else "never",
            "draft_pr": "on_success" if draft_pr_on_success else "never",
            "remote": "origin",
            "pr_title": str(pr_title or ""),
            "pr_body": str(pr_body or ""),
        },
        "budget_policy": {
            "max_cycles": int(max_cycles or getattr(S, "CODING_AGENT_MAX_CYCLES_PER_RUN", 1000)),
            "max_runtime_sec": int(max_runtime_sec or getattr(S, "CODING_AGENT_MAX_RUNTIME_SEC", 21600)),
        },
        "context_policy": {
            "context_reset_cycles": int(context_reset_cycles or 0),
            "context_reset_chars": int(getattr(S, "CODING_AGENT_CONTEXT_RESET_CHARS", 64_000)),
            "state_snapshot_on_reset": True,
        },
    }


def save_task(task: Dict[str, Any]) -> Dict[str, Any]:
    path = _task_path(str(task.get("id") or ""))
    with _json_lock(path):
        _raise_if_task_deleted(path)
        task["updated_at"] = _now()
        _write_json(path, task)
        return task


def mutate_task(task_id: str, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Atomically read, mutate, and persist task metadata.

    Coding runs write events while users can send guidance from another thread.
    Holding the task's re-entrant JSON lock for the full transaction prevents a
    later writer from silently replacing the other writer's update.
    """

    path = _task_path(task_id)
    with _json_lock(path):
        task = _read_json(path)
        mutator(task)
        _raise_if_task_deleted(path)
        task["updated_at"] = _now()
        _write_json(path, task)
        return task


def normalize_project_plan(raw: Any, *, fallback_goal: str = "") -> Dict[str, Any]:
    plan = raw if isinstance(raw, dict) else {}
    goal = str(plan.get("goal") or fallback_goal or "").strip()[:4_000]
    raw_items = plan.get("items") if isinstance(plan.get("items"), list) else []
    items: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_items[:80]):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("summary") or "").strip()[:500]
        if not title:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status not in _PROJECT_PLAN_STATUSES:
            status = "pending"
        item_id = str(item.get("id") or f"step-{index + 1}").strip()[:120] or f"step-{index + 1}"
        items.append(
            {
                "id": item_id,
                "title": title,
                "status": status,
                "summary": str(item.get("summary") or "").strip()[:2_000],
            }
        )
    counts = {status: 0 for status in sorted(_PROJECT_PLAN_STATUSES)}
    for item in items:
        counts[str(item["status"])] += 1
    done = counts["completed"] + counts["skipped"]
    try:
        revision = max(0, int(plan.get("revision") or 0))
    except Exception:
        revision = 0
    return {
        "goal": goal,
        "items": items,
        "counts": {**counts, "total": len(items), "done": done},
        "revision": revision,
        "updated_at": plan.get("updated_at"),
        "updated_by": str(plan.get("updated_by") or "").strip()[:120],
        "note": str(plan.get("note") or "").strip()[:2_000],
    }


def update_project_plan(
    task_id: str,
    *,
    goal: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    note: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _update_project_plan(
            task_id,
            goal=goal,
            items=items,
            note=note,
            actor=actor,
        )


def _update_project_plan(
    task_id: str,
    *,
    goal: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    note: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    if goal is None and items is None and note is None:
        raise HTTPException(status_code=400, detail="plan update requires goal, items, or note")
    if items is not None and not isinstance(items, list):
        raise HTTPException(status_code=400, detail="plan items must be an array")

    def apply(task: Dict[str, Any]) -> None:
        current = normalize_project_plan(task.get("project_plan"), fallback_goal=str(task.get("prompt") or ""))
        candidate = dict(current)
        if goal is not None:
            candidate["goal"] = str(goal or "").strip()
        if items is not None:
            candidate["items"] = items
        if note is not None:
            candidate["note"] = str(note or "").strip()
        candidate["revision"] = int(current.get("revision") or 0) + 1
        candidate["updated_at"] = _now()
        candidate["updated_by"] = str(actor or "coding-agent").strip() or "coding-agent"
        task["project_plan"] = normalize_project_plan(candidate, fallback_goal=str(task.get("prompt") or ""))

    task = mutate_task(task_id, apply)
    return {"ok": True, "plan": normalize_project_plan(task.get("project_plan"), fallback_goal=str(task.get("prompt") or ""))}


def append_guidance_message(
    task_id: str,
    *,
    message: str,
    actor: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _append_guidance_message(
            task_id,
            message=message,
            actor=actor,
            run_id=run_id,
        )


def _append_guidance_message(
    task_id: str,
    *,
    message: str,
    actor: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    text = str(message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")

    def apply(task: Dict[str, Any]) -> None:
        messages = task.get("guidance_messages")
        if not isinstance(messages, list):
            messages = []
        now = _now()
        messages.append(
            {
                "ts": now,
                "role": "user",
                "actor": str(actor or "").strip(),
                "run_id": str(run_id or "").strip(),
                "content": text,
            }
        )
        task["guidance_messages"] = messages[-200:]
        task["last_guidance_at"] = now

    task = mutate_task(task_id, apply)
    return public_task(task)


def set_task_coding_model(
    task_id: str,
    *,
    coding_model: Optional[str],
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _set_task_coding_model(task_id, coding_model=coding_model)


def _set_task_coding_model(
    task_id: str,
    *,
    coding_model: Optional[str],
) -> Dict[str, Any]:
    next_model = str(coding_model or "").strip()
    def apply(task: Dict[str, Any]) -> None:
        agent_status = str(task.get("agent_status") or "").strip().lower()
        if agent_status in {"queued", "running", "stopping", "pausing"}:
            raise HTTPException(status_code=409, detail="cannot change coding model while the agent is active")
        previous_model = str(task.get("coding_model") or "").strip()
        if previous_model == next_model:
            return
        task["coding_model"] = next_model
        events = task.get("agent_events")
        if not isinstance(events, list):
            events = []
        events.append(
            {
                "ts": _now(),
                "type": "model_updated",
                "summary": f"Workspace coding model set to {next_model or 'default'}.",
                "previous_model": previous_model,
                "model": next_model,
            }
        )
        task["agent_events"] = events[-80:]

    task = mutate_task(task_id, apply)
    return public_task(task)


def list_tasks(limit: int = 100) -> List[Dict[str, Any]]:
    _ensure_enabled()
    _ensure_dirs()
    items: List[Dict[str, Any]] = []
    for path in tasks_dir().glob("code_*.json"):
        try:
            item = _read_json(path)
            items.append(public_task(item, include_commands=False))
        except Exception:
            continue
    items.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
    return items[: max(1, min(int(limit or 100), 500))]


def recover_interrupted_agent_runs() -> Dict[str, Any]:
    if not coding_enabled():
        return {"ok": True, "recovered": 0, "tasks": []}
    _ensure_dirs()
    recovered: List[str] = []
    for path in tasks_dir().glob("code_*.json"):
        try:
            task = _read_json(path)
        except Exception:
            continue
        status = str(task.get("agent_status") or "").strip().lower()
        if status == "interrupted" and bool(task.get("agent_auto_resume_pending")):
            recovered.append(str(task.get("id") or path.stem))
            continue
        if status not in {"queued", "running", "stopping", "pausing"}:
            continue
        events = task.get("agent_events")
        if not isinstance(events, list):
            events = []
        ev = {
            "ts": _now(),
            "type": "interrupted",
            "summary": (
                "Gateway restarted while this coding run was active. Nexus will automatically resume it from the "
                "durable controller snapshot, latest checkpoint commit, and current git state."
            ),
            "previous_status": status,
            "run_id": task.get("agent_run_id") or "",
            "stop_reason_code": "gateway_restart",
        }
        events.append(ev)
        task["agent_events"] = events[-max(20, min(int(getattr(S, "CODING_AGENT_MAX_EVENTS", 1000) or 1000), 1000)) :]
        task["agent_previous_status"] = status
        task["agent_status"] = "interrupted"
        task["agent_auto_resume_pending"] = True
        task["agent_summary"] = ev["summary"]
        task["agent_error"] = ev["summary"]
        task["agent_stop_reason_code"] = "gateway_restart"
        task["agent_finished_at"] = _now()
        task["agent_last_event_at"] = ev["ts"]
        runs = task.get("agent_runs")
        if isinstance(runs, list):
            current_run_id = str(task.get("agent_run_id") or "")
            for record in reversed(runs):
                if isinstance(record, dict) and str(record.get("run_id") or "") == current_run_id:
                    record.update(
                        {
                            "status": "interrupted",
                            "finished_at": task["agent_finished_at"],
                            "cycle": int(task.get("agent_cycle") or 0),
                            "summary": ev["summary"],
                            "error": ev["summary"],
                            "stop_reason_code": "gateway_restart",
                        }
                    )
                    break
            task["agent_runs"] = [item for item in runs[-200:] if isinstance(item, dict)]
        save_task(task)
        recovered.append(str(task.get("id") or path.stem))
    return {"ok": True, "recovered": len(recovered), "tasks": recovered}


def _effective_git_token(token_value: Optional[str] = None) -> str:
    if token_value is not None:
        return str(token_value or "").strip()
    return git_token()


def _redact_text(value: str, *, extra_tokens: Optional[Sequence[str]] = None) -> str:
    out = str(value or "")
    tokens = [git_token()]
    if extra_tokens:
        tokens.extend(str(item or "").strip() for item in extra_tokens)
    for token in tokens:
        if token:
            out = out.replace(token, "***")
    out = re.sub(
        r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1***",
        out,
    )
    out = re.sub(
        r"(?i)(\b(?:access_token|api_key|password|secret|token)\s*[=:]\s*)[^\s,;]+",
        r"\1***",
        out,
    )
    out = re.sub(
        r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|hf_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})\b",
        "***",
        out,
    )
    return out


def redact_repo_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return _redact_text(raw)


def _reject_url_credentials(url: str) -> None:
    try:
        parts = urlsplit(url)
    except Exception:
        return
    if parts.scheme and (parts.username or parts.password):
        raise HTTPException(status_code=400, detail="put git credentials in gateway env, not in repo_url")


def _normalize_repo_for_match(url: str) -> str:
    out = str(url or "").strip()
    while out.endswith("/") and not out.endswith("://"):
        out = out[:-1]
    return out


def _allowed_repo_patterns() -> List[str]:
    patterns: List[str] = []
    raw_json = str(getattr(S, "CODING_ALLOWED_REPOS_JSON", "") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                patterns.extend(str(item).strip() for item in parsed if str(item).strip())
            elif isinstance(parsed, dict):
                values = parsed.get("repos")
                if isinstance(values, list):
                    patterns.extend(str(item).strip() for item in values if str(item).strip())
        except Exception:
            logger.warning("CODING_ALLOWED_REPOS_JSON could not be parsed")
    raw = str(getattr(S, "CODING_ALLOWED_REPOS", "") or "").strip()
    if raw:
        patterns.extend(item.strip() for item in raw.split(",") if item.strip())
    if not patterns:
        default_repo = str(getattr(S, "CODING_DEFAULT_REPO_URL", "") or "").strip()
        if default_repo:
            patterns.append(default_repo)
    return [_normalize_repo_for_match(item) for item in patterns if item]


def allowed_repos_public() -> List[str]:
    return [redact_repo_url(item) for item in _allowed_repo_patterns()]


def _is_github_url(url: str) -> bool:
    raw = str(url or "").strip()
    if raw.startswith("git@github.com:"):
        return True
    try:
        parts = urlsplit(raw)
    except Exception:
        return False
    return parts.scheme in {"https", "ssh", "git"} and (parts.hostname or "").lower() == "github.com"


def _repo_allowed(url: str) -> bool:
    wanted = _normalize_repo_for_match(url)
    for pattern in _allowed_repo_patterns():
        if pattern == "*":
            return _is_github_url(wanted)
        if pattern.endswith("*") and wanted.startswith(pattern[:-1]):
            return True
        if wanted == pattern:
            return True
    return False


def default_repo_url() -> str:
    return str(getattr(S, "CODING_DEFAULT_REPO_URL", "") or "").strip()


def _resolve_repo_url(repo_url: Optional[str]) -> str:
    repo = str(repo_url or "").strip() or default_repo_url()
    if not repo:
        raise HTTPException(status_code=400, detail="repo_url is required")
    _reject_url_credentials(repo)
    if not _repo_allowed(repo):
        raise HTTPException(status_code=403, detail="repo_url is not in CODING_ALLOWED_REPOS")
    return repo


def _resolve_model_integration_repo_url(repo_url: Optional[str]) -> str:
    repo = str(repo_url or "").strip() or default_repo_url()
    if not repo:
        raise HTTPException(status_code=400, detail="destination repo_url is required for model integration workspaces")
    _reject_url_credentials(repo)
    if not _is_github_url(repo):
        raise HTTPException(status_code=400, detail="model integration destination repo_url must be a GitHub repository URL")
    return repo


def _safe_branch(value: Optional[str], *, task_id: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        prefix = str(getattr(S, "CODING_BRANCH_PREFIX", "") or "nexus-coder").strip().strip("/") or "nexus-coder"
        return f"{prefix}/{task_id}"
    cleaned = _SAFE_REF_RE.sub("-", raw).strip("/.")
    cleaned = re.sub(r"/+", "/", cleaned)
    if not cleaned or cleaned.endswith(".lock") or ".." in cleaned or "@{" in cleaned:
        raise HTTPException(status_code=400, detail="invalid branch_name")
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip("/.")
    return cleaned


def _base_branch(value: Optional[str]) -> str:
    raw = str(value or "").strip() or str(getattr(S, "CODING_DEFAULT_BASE_BRANCH", "") or "main").strip()
    cleaned = _SAFE_REF_RE.sub("-", raw).strip("/.")
    if not cleaned or cleaned.endswith(".lock") or ".." in cleaned or "@{" in cleaned:
        raise HTTPException(status_code=400, detail="invalid base_branch")
    return cleaned


def new_task_id() -> str:
    return f"code_{secrets.token_hex(6)}"


def git_token() -> str:
    return (
        str(getattr(S, "CODING_GIT_TOKEN", "") or "").strip()
        or str(os.environ.get("GITHUB_TOKEN") or "").strip()
        or str(os.environ.get("GH_TOKEN") or "").strip()
    )


def _base_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key in ["PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"]:
        val = os.environ.get(key)
        if val:
            env[key] = val
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", "/tmp")
    env.setdefault("LANG", "C.UTF-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    author_name = str(getattr(S, "CODING_GIT_AUTHOR_NAME", "") or "Nexus Coding Agent").strip() or "Nexus Coding Agent"
    author_email = str(getattr(S, "CODING_GIT_AUTHOR_EMAIL", "") or "nexus-coder@localhost").strip() or "nexus-coder@localhost"
    env.setdefault("GIT_AUTHOR_NAME", author_name)
    env.setdefault("GIT_COMMITTER_NAME", author_name)
    env.setdefault("GIT_AUTHOR_EMAIL", author_email)
    env.setdefault("GIT_COMMITTER_EMAIL", author_email)
    return env


class _GitCredentialEnv:
    def __init__(self, enabled: bool, *, git_token_value: Optional[str] = None) -> None:
        self.enabled = enabled
        self.git_token_value = git_token_value
        self.tmpdir: Optional[tempfile.TemporaryDirectory[str]] = None
        self.path = ""

    def __enter__(self) -> Dict[str, str]:
        token = _effective_git_token(self.git_token_value) if self.enabled else ""
        if not token:
            return {}
        username = str(getattr(S, "CODING_GIT_USERNAME", "") or "x-access-token").strip() or "x-access-token"
        self.tmpdir = tempfile.TemporaryDirectory(prefix="nexus-coding-git-")
        askpass = Path(self.tmpdir.name).joinpath("askpass.sh")
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' \"$GIT_USERNAME\" ;;\n"
            "  *) printf '%s\\n' \"$GIT_PASSWORD\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        try:
            askpass.chmod(0o700)
        except Exception:
            pass
        self.path = str(askpass)
        return {
            "GIT_ASKPASS": self.path,
            "GIT_USERNAME": username,
            "GIT_PASSWORD": token,
            "GH_TOKEN": token,
            "GITHUB_TOKEN": token,
        }

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.tmpdir is not None:
            self.tmpdir.cleanup()


def _truncate(value: str, limit: int, *, extra_tokens: Optional[Sequence[str]] = None) -> Tuple[str, bool]:
    text = _redact_text(value or "", extra_tokens=extra_tokens)
    if len(text) <= limit:
        return text, False
    half = max(100, limit // 2)
    return f"{text[:half]}\n\n[... truncated {len(text) - limit} chars ...]\n\n{text[-half:]}", True


def _redact_argv(argv: Sequence[str], *, extra_tokens: Optional[Sequence[str]] = None) -> List[str]:
    tokens = [git_token()]
    if extra_tokens:
        tokens.extend(str(item or "").strip() for item in extra_tokens)
    out: List[str] = []
    skip_next = False
    for item in argv:
        value = str(item)
        lower = value.lower()
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if lower in {"--password", "--token", "--secret"}:
            out.append(value)
            skip_next = True
            continue
        for token in tokens:
            if token and token in value:
                value = value.replace(token, "***")
        out.append(value)
    return out


def _git_safe_directory_for_cwd(cwd: Path) -> str:
    current = Path(cwd).resolve()
    while True:
        if current.joinpath(".git").exists():
            return str(current)
        parent = current.parent
        if parent == current:
            return str(Path(cwd).resolve())
        current = parent


def _argv_with_git_safe_directory(argv: Sequence[str], *, cwd: Path) -> List[str]:
    if not argv:
        return [str(item) for item in argv]
    first = Path(str(argv[0])).name.lower()
    if first != "git":
        return [str(item) for item in argv]
    safe_directory = _git_safe_directory_for_cwd(cwd)
    return [str(argv[0]), "-c", f"safe.directory={safe_directory}", *[str(item) for item in argv[1:]]]


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # The supervisor may need both a graceful and forced descendant sweep.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_group_alive(pgid):
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and _process_group_alive(pgid):
        time.sleep(0.02)


class _BoundedPipeCapture:
    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = max(1_000, int(limit_bytes))
        self.head_limit = self.limit_bytes // 2
        self.tail_limit = self.limit_bytes - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0
        self.error = ""

    def drain(self, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                self.total_bytes += len(chunk)
                head_remaining = self.head_limit - len(self.head)
                if head_remaining > 0:
                    self.head.extend(chunk[:head_remaining])
                    chunk = chunk[head_remaining:]
                if chunk and self.tail_limit > 0:
                    self.tail.extend(chunk)
                    if len(self.tail) > self.tail_limit:
                        del self.tail[: len(self.tail) - self.tail_limit]
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.limit_bytes

    def text(self, *, decode_errors: str) -> str:
        if not self.truncated:
            return bytes(self.head + self.tail).decode("utf-8", errors=decode_errors)
        head = bytes(self.head).decode("utf-8", errors="replace")
        tail = bytes(self.tail).decode("utf-8", errors="replace")
        omitted = self.total_bytes - self.limit_bytes
        return f"{head}\n\n[... truncated {omitted} output bytes ...]\n\n{tail}"


def _stage_validation_workspace(source: Path, destination: Path) -> tuple[int, int]:
    destination.mkdir(mode=0o700)
    source_root = source.resolve()
    destination_root = destination.resolve()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    pending = [(os.open(source, directory_flags | nofollow), destination)]
    staged_inodes: Dict[tuple[int, int], Path] = {}
    total_bytes = 0
    total_entries = 0
    try:
        while pending:
            source_fd, destination_dir = pending.pop()
            try:
                with os.scandir(source_fd) as entries:
                    for entry in entries:
                        total_entries += 1
                        if total_entries > _HARNESS_VALIDATION_STAGE_ENTRIES:
                            raise RuntimeError(
                                "validation workspace staging entry limit exceeded"
                            )
                        destination_path = destination_dir.joinpath(entry.name)
                        if entry.is_symlink():
                            link_target = os.readlink(entry.name, dir_fd=source_fd)
                            staged_link_target = link_target
                            if os.path.isabs(link_target):
                                try:
                                    target_relative = Path(
                                        os.path.normpath(link_target)
                                    ).relative_to(source_root)
                                except ValueError:
                                    pass
                                else:
                                    staged_link_target = str(
                                        destination_root.joinpath(target_relative)
                                    )
                            total_bytes += len(os.fsencode(staged_link_target))
                            if total_bytes > _HARNESS_VALIDATION_STAGE_BYTES:
                                raise RuntimeError(
                                    "validation workspace staging byte limit exceeded"
                                )
                            destination_path.symlink_to(staged_link_target)
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            child_fd = os.open(
                                entry.name,
                                directory_flags | nofollow,
                                dir_fd=source_fd,
                            )
                            destination_path.mkdir(mode=0o700)
                            pending.append((child_fd, destination_path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            raise RuntimeError(
                                "validation workspace contains a special file"
                            )
                        descriptor = os.open(
                            entry.name,
                            os.O_RDONLY | nofollow | nonblock,
                            dir_fd=source_fd,
                        )
                        with os.fdopen(descriptor, "rb") as reader:
                            source_mode = os.fstat(reader.fileno()).st_mode
                            if not stat.S_ISREG(source_mode):
                                raise RuntimeError(
                                    "validation workspace contains a special file"
                                )
                            source_stat = os.fstat(reader.fileno())
                            inode_key = (source_stat.st_dev, source_stat.st_ino)
                            existing_destination = staged_inodes.get(inode_key)
                            if existing_destination is not None:
                                os.link(
                                    existing_destination,
                                    destination_path,
                                    follow_symlinks=False,
                                )
                                continue
                            with destination_path.open("xb") as writer:
                                while True:
                                    chunk = reader.read(64 * 1024)
                                    if not chunk:
                                        break
                                    total_bytes += len(chunk)
                                    if total_bytes > _HARNESS_VALIDATION_STAGE_BYTES:
                                        raise RuntimeError(
                                            "validation workspace staging byte limit exceeded"
                                        )
                                    writer.write(chunk)
                        destination_path.chmod(source_mode & 0o777)
                        staged_inodes[inode_key] = destination_path
            finally:
                os.close(source_fd)
    finally:
        for source_fd, _ in pending:
            os.close(source_fd)
    return total_bytes, total_entries


def _validation_tmp_parent() -> Optional[str]:
    raw = os.environ.get("NEXUS_VALIDATION_TMP_ROOT", "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise OSError("validation temporary filesystem is unavailable")
    return str(root.resolve())


def _create_staged_validation_tree(
    source: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, int, int]:
    temporary = tempfile.TemporaryDirectory(
        prefix="nexus-validation-",
        dir=_validation_tmp_parent(),
    )
    try:
        root = Path(temporary.name).resolve()
        root.chmod(0o711)
        root.joinpath("scratch").mkdir(mode=0o700)
        workspace = root.joinpath("workspace")
        baseline_bytes, baseline_entries = _stage_validation_workspace(
            source,
            workspace,
        )
        # The root scan also sees the scratch and workspace directories.
        return temporary, root, workspace, baseline_bytes, baseline_entries + 2
    except BaseException:
        temporary.cleanup()
        raise


def _run_contained_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: float,
    decode_errors: str,
    output_limit_chars: int,
    validation_workspace: Optional[Path] = None,
    validation_baseline_bytes: Optional[int] = None,
    validation_baseline_entries: Optional[int] = None,
) -> Tuple[Optional[int], str, str, bool, str, bool, bool]:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        return None, "", "", False, "descendant process containment requires a Linux host", False, False
    supervisor = Path(__file__).with_name("coding_process_supervisor.py")
    proc: Optional[subprocess.Popen[bytes]] = None
    timed_out = False
    containment_error = ""
    stdout = ""
    stderr = ""
    capture_limit_bytes = max(4_096, int(output_limit_chars) * 4)
    stdout_capture = _BoundedPipeCapture(capture_limit_bytes)
    stderr_capture = _BoundedPipeCapture(capture_limit_bytes)
    capture_threads: List[threading.Thread] = []
    owned_tree: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if validation_workspace is None:
            (
                owned_tree,
                scratch_root,
                validation_workspace,
                baseline_bytes,
                baseline_entries,
            ) = _create_staged_validation_tree(cwd)
            command_cwd = validation_workspace
        else:
            validation_workspace = validation_workspace.resolve()
            scratch_root = validation_workspace.parent
            scratch_path = scratch_root.joinpath("scratch")
            if (
                not validation_workspace.is_dir()
                or validation_workspace.is_symlink()
                or not scratch_path.is_dir()
                or scratch_path.is_symlink()
            ):
                raise OSError("persistent validation workspace is unavailable")
            command_cwd = _ensure_inside(validation_workspace, cwd)
            if not command_cwd.is_dir():
                raise OSError("validation working directory is unavailable")
            if validation_baseline_bytes is None or validation_baseline_entries is None:
                raise RuntimeError("validation workspace baseline is unavailable")
            baseline_bytes = max(0, int(validation_baseline_bytes))
            baseline_entries = max(0, int(validation_baseline_entries))
        scratch_path = scratch_root.joinpath("scratch")
        contained_env = dict(env)
        contained_env.update({
            "HOME": str(scratch_path),
            "TMPDIR": str(scratch_path),
            "NEXUS_VALIDATION_SCRATCH": str(scratch_root),
            "NEXUS_VALIDATION_WORKSPACE": str(validation_workspace),
            "NEXUS_VALIDATION_FILE_BYTES": str(_HARNESS_VALIDATION_FILE_BYTES),
            "NEXUS_VALIDATION_OPEN_FILES": str(_HARNESS_VALIDATION_OPEN_FILES),
            "NEXUS_VALIDATION_PROCESSES": str(_HARNESS_VALIDATION_PROCESSES),
            "NEXUS_VALIDATION_MEMORY_BYTES": str(_HARNESS_VALIDATION_MEMORY_BYTES),
            "NEXUS_VALIDATION_AGGREGATE_MEMORY_BYTES": str(
                _HARNESS_VALIDATION_AGGREGATE_MEMORY_BYTES
            ),
            "NEXUS_VALIDATION_SCRATCH_BYTES": str(_HARNESS_VALIDATION_SCRATCH_BYTES),
            "NEXUS_VALIDATION_SCRATCH_ENTRIES": str(_HARNESS_VALIDATION_SCRATCH_ENTRIES),
            "NEXUS_VALIDATION_BASELINE_BYTES": str(baseline_bytes),
            "NEXUS_VALIDATION_BASELINE_ENTRIES": str(baseline_entries),
        })
        validation_cgroup_root = os.environ.get("NEXUS_VALIDATION_CGROUP_ROOT", "").strip()
        if validation_cgroup_root:
            contained_env["NEXUS_VALIDATION_CGROUP_ROOT"] = validation_cgroup_root
        if os.environ.get("PYTEST_CURRENT_TEST"):
            # Local and CI pytest runs generally do not own a delegated cgroup.
            # Production never receives this test-only escape hatch.
            contained_env["NEXUS_TEST_VALIDATION_ALLOW_POLLING"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(supervisor), *[str(item) for item in argv]],
            cwd=str(command_cwd),
            env=contained_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        capture_threads = [
            threading.Thread(target=stdout_capture.drain, args=(proc.stdout,), daemon=True),
            threading.Thread(target=stderr_capture.drain, args=(proc.stderr,), daemon=True),
        ]
        for thread in capture_threads:
            thread.start()
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(proc.pid)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1.0)
        for thread in capture_threads:
            thread.join(timeout=1.0)
        if any(thread.is_alive() for thread in capture_threads):
            proc.stdout.close()
            proc.stderr.close()
            for thread in capture_threads:
                thread.join(timeout=0.5)
            containment_error = "validation output streams remained open after containment"
        stdout = stdout_capture.text(decode_errors=decode_errors)
        stderr = stderr_capture.text(decode_errors=decode_errors)
        if stdout_capture.error or stderr_capture.error:
            containment_error = containment_error or "could not drain validation output safely"
        if proc.returncode == 125 and "NEXUS_CONTAINMENT_ERROR:" in stderr:
            containment_error = "validation process containment failed"
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        containment_error = f"could not launch contained process: {exc}"
    finally:
        if owned_tree is not None:
            try:
                owned_tree.cleanup()
            except OSError as exc:
                containment_error = containment_error or f"could not clean validation scratch: {exc}"
    return (
        None if timed_out or proc is None else proc.returncode,
        stdout,
        stderr,
        timed_out,
        containment_error,
        stdout_capture.truncated,
        stderr_capture.truncated,
    )


def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_sec: Optional[float] = None,
    timeout_limit_sec: Optional[float] = None,
    use_git_credentials: bool = False,
    git_token_value: Optional[str] = None,
    env_overrides: Optional[Dict[str, str]] = None,
    decode_errors: str = "strict",
    output_limit_chars: Optional[int] = None,
    isolate_process_group: bool = False,
    validation_workspace: Optional[Path] = None,
    validation_baseline_bytes: Optional[int] = None,
    validation_baseline_entries: Optional[int] = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    if output_limit_chars is None:
        limit = max_output_chars()
    else:
        try:
            limit = max(1_000, min(_HARNESS_MAX_DIFF_CHARS, int(output_limit_chars)))
        except Exception:
            limit = max_output_chars()
    if timeout_limit_sec is None:
        effective_timeout_sec = command_timeout_sec(timeout_sec)
    else:
        try:
            timeout_limit = max(1.0, min(float(timeout_limit_sec), 3600.0))
            requested_timeout = timeout_limit if timeout_sec is None else float(timeout_sec)
        except Exception:
            timeout_limit = 3600.0
            requested_timeout = timeout_limit
        effective_timeout_sec = max(1.0, min(requested_timeout, timeout_limit, 3600.0))
    env = _base_env()
    redaction_tokens = [_effective_git_token(git_token_value)] if use_git_credentials else []
    effective_argv = _argv_with_git_safe_directory(argv, cwd=cwd)
    with _GitCredentialEnv(use_git_credentials, git_token_value=git_token_value) as extra_env:
        env.update(extra_env)
        if env_overrides:
            env.update({str(key): str(value) for key, value in env_overrides.items()})
        if isolate_process_group:
            (
                returncode,
                raw_stdout,
                raw_stderr,
                timed_out,
                containment_error,
                stdout_capture_truncated,
                stderr_capture_truncated,
            ) = _run_contained_process(
                effective_argv,
                cwd=cwd,
                env=env,
                timeout_sec=effective_timeout_sec,
                decode_errors=decode_errors,
                output_limit_chars=limit,
                validation_workspace=validation_workspace,
                validation_baseline_bytes=validation_baseline_bytes,
                validation_baseline_entries=validation_baseline_entries,
            )
            stdout, stdout_truncated = _truncate(raw_stdout, limit, extra_tokens=redaction_tokens)
            stderr, stderr_truncated = _truncate(raw_stderr, limit, extra_tokens=redaction_tokens)
            stdout_truncated = bool(stdout_truncated or stdout_capture_truncated)
            stderr_truncated = bool(stderr_truncated or stderr_capture_truncated)
            if timed_out:
                stderr = stderr or f"timeout after {effective_timeout_sec:.0f}s"
            if containment_error:
                stderr = f"{containment_error}\n{stderr}".rstrip()
            return {
                "ok": bool(returncode == 0 and not timed_out and not containment_error),
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "argv": _redact_argv(argv, extra_tokens=redaction_tokens),
                "cwd": str(cwd),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }
        try:
            proc = subprocess.run(
                effective_argv,
                cwd=str(cwd),
                env=env,
                encoding="utf-8",
                errors=decode_errors,
                capture_output=True,
                timeout=effective_timeout_sec,
            )
            stdout, stdout_truncated = _truncate(proc.stdout or "", limit, extra_tokens=redaction_tokens)
            stderr, stderr_truncated = _truncate(proc.stderr or "", limit, extra_tokens=redaction_tokens)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "argv": _redact_argv(argv, extra_tokens=redaction_tokens),
                "cwd": str(cwd),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = _truncate(exc.stdout or "", limit, extra_tokens=redaction_tokens)
            stderr, stderr_truncated = _truncate(exc.stderr or "", limit, extra_tokens=redaction_tokens)
            return {
                "ok": False,
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr or f"timeout after {effective_timeout_sec:.0f}s",
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "argv": _redact_argv(argv, extra_tokens=redaction_tokens),
                "cwd": str(cwd),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "returncode": None,
                "stdout": "",
                "stderr": f"command not found: {argv[0]}",
                "argv": _redact_argv(argv, extra_tokens=redaction_tokens),
                "cwd": str(cwd),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }


def _command_summary(result: Dict[str, Any], *, label: str) -> Dict[str, Any]:
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    return {
        "ts": _now(),
        "label": label,
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "argv": result.get("argv") or [],
        "duration_ms": result.get("duration_ms"),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def _append_command(task: Dict[str, Any], result: Dict[str, Any], *, label: str) -> None:
    commands = task.get("commands")
    if not isinstance(commands, list):
        commands = []
    commands.append(_command_summary(result, label=label))
    task["commands"] = commands[-30:]
    task["last_command_at"] = _now()


def _task_workspace(task_id: str) -> Path:
    return workspace_root().joinpath(task_id).resolve()


def _repo_path_for(task_id: str) -> Path:
    return _task_workspace(task_id).joinpath("repo").resolve()


def create_task(
    *,
    repo_url: Optional[str],
    base_branch: Optional[str],
    branch_name: Optional[str],
    prompt: Optional[str],
    owner: Optional[str],
    owner_user_id: Optional[int] = None,
    git_token_value: Optional[str] = None,
    coding_model: Optional[str] = None,
    mission_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _ensure_enabled()
    _ensure_dirs()
    repo = _resolve_repo_url(repo_url)
    task_id = new_task_id()
    branch = _safe_branch(branch_name, task_id=task_id)
    base = _base_branch(base_branch)
    workspace = _task_workspace(task_id)
    repo_path = _repo_path_for(task_id)
    task = {
        "schema": SCHEMA,
        "id": task_id,
        "status": "initializing",
        "created_at": _now(),
        "updated_at": _now(),
        "owner": owner or "unknown",
        "owner_user_id": owner_user_id,
        "repo_url": repo,
        "base_branch": base,
        "branch_name": branch,
        "prompt": str(prompt or "").strip(),
        "coding_model": str(coding_model or "").strip(),
        "workspace_path": str(workspace),
        "repo_path": str(repo_path),
        "commands": [],
        "project_plan": normalize_project_plan({"goal": str(prompt or "").strip(), "items": []}),
        "agent_runs": [],
    }
    task["mission"] = normalize_coding_mission(task, mission_overrides)
    save_task(task)

    try:
        workspace.mkdir(parents=True, exist_ok=True)
        clone_argv = ["git", "clone", "--depth", "1", "--branch", base, repo, str(repo_path)]
        clone_result = _run_process(
            clone_argv,
            cwd=workspace,
            timeout_sec=max(command_timeout_sec(), 300.0),
            use_git_credentials=True,
            git_token_value=git_token_value,
        )
        _append_command(task, clone_result, label="clone")
        if not clone_result.get("ok"):
            task["status"] = "error"
            task["error"] = "git clone failed"
            save_task(task)
            return public_task(task)

        switch_result = _run_process(["git", "switch", "-c", branch], cwd=repo_path, use_git_credentials=False)
        if not switch_result.get("ok"):
            switch_result = _run_process(["git", "checkout", "-b", branch], cwd=repo_path, use_git_credentials=False)
        _append_command(task, switch_result, label="branch")
        if not switch_result.get("ok"):
            task["status"] = "error"
            task["error"] = "branch creation failed"
            save_task(task)
            return public_task(task)

        task["status"] = "ready"
        task.pop("error", None)
        save_task(task)
        return public_task(task)
    except Exception as exc:
        logger.warning("coding task create failed id=%s error=%s", task_id, exc)
        task["status"] = "error"
        task["error"] = f"{type(exc).__name__}: {_redact_text(str(exc), extra_tokens=[_effective_git_token(git_token_value)])}"
        save_task(task)
        return public_task(task)


def _normalize_harness_fixture_files(
    fixture_id: str,
    files: Dict[str, str],
) -> Tuple[str, Dict[str, str]]:
    normalized_id = str(fixture_id or "").strip()
    if not _HARNESS_FIXTURE_ID_RE.fullmatch(normalized_id):
        raise HTTPException(status_code=400, detail="invalid harness fixture_id")
    if not isinstance(files, dict) or not files:
        raise HTTPException(status_code=400, detail="harness fixture files must be a non-empty object")
    if len(files) > _HARNESS_MAX_FILES:
        raise HTTPException(status_code=413, detail="harness fixture has too many files")

    normalized: Dict[str, str] = {}
    total_bytes = 0
    per_file_limit = _HARNESS_MAX_FILE_BYTES
    for raw_path, raw_content in files.items():
        path = str(raw_path or "")
        if (
            not path
            or len(path.encode("utf-8")) > 4096
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise HTTPException(status_code=400, detail=f"unsafe harness fixture path: {raw_path}")
        parts = path.split("/")
        if any(
            not part
            or part in {".", ".."}
            or part.casefold() in _HARNESS_RESERVED_PATH_PARTS
            for part in parts
        ):
            raise HTTPException(status_code=400, detail=f"unsafe harness fixture path: {raw_path}")
        if not isinstance(raw_content, str):
            raise HTTPException(status_code=400, detail=f"harness fixture file must contain text: {path}")
        size = len(raw_content.encode("utf-8"))
        if size > per_file_limit:
            raise HTTPException(status_code=413, detail=f"harness fixture file is too large: {path}")
        total_bytes += size
        if total_bytes > _HARNESS_MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="harness fixture files are too large in aggregate")
        normalized[path] = raw_content
    return normalized_id, normalized


def create_harness_task(
    *,
    fixture_id: str,
    files: Dict[str, str],
    prompt: str,
    owner: Optional[str],
    owner_user_id: Optional[int] = None,
    coding_model: Optional[str] = None,
    mission_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a local-only disposable Git workspace for a coding harness fixture."""
    _ensure_enabled()
    normalized_id, normalized_files = _normalize_harness_fixture_files(fixture_id, files)
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt:
        raise HTTPException(status_code=400, detail="harness fixture prompt is required")
    if len(normalized_prompt.encode("utf-8")) > _HARNESS_MAX_PROMPT_BYTES:
        raise HTTPException(status_code=413, detail="harness fixture prompt is too large")

    _ensure_dirs()
    task_id = new_task_id()
    base = "main"
    branch = f"nexus-coding-harness/{task_id}"
    workspace = _task_workspace(task_id)
    repo_path = _repo_path_for(task_id)
    task = {
        "schema": SCHEMA,
        "id": task_id,
        "kind": "harness_eval",
        "status": "initializing",
        "created_at": _now(),
        "updated_at": _now(),
        "owner": owner or "unknown",
        "owner_user_id": owner_user_id,
        "repo_url": f"harness-fixture://{normalized_id}",
        "base_branch": base,
        "branch_name": branch,
        "prompt": normalized_prompt,
        "coding_model": str(coding_model or "").strip(),
        "workspace_path": str(workspace),
        "repo_path": str(repo_path),
        "seed_files": sorted(normalized_files),
        "harness_fixture": {
            "id": normalized_id,
            "file_count": len(normalized_files),
            "total_bytes": sum(len(content.encode("utf-8")) for content in normalized_files.values()),
        },
        "commands": [],
        "project_plan": normalize_project_plan({"goal": normalized_prompt, "items": []}),
        "agent_runs": [],
    }
    task["mission"] = normalize_coding_mission(task, mission_overrides)
    task["harness_expires_at"] = int(
        task["created_at"]
        + min(
            86_400,
            max(900, int(task["mission"]["budget_policy"]["max_runtime_sec"]) + 900),
        )
    )
    save_task(task)

    try:
        workspace.mkdir(parents=True, exist_ok=False)
        repo_path.mkdir()
        root = repo_path.resolve()
        for rel, content in normalized_files.items():
            target = repo_path.joinpath(*rel.split("/")).resolve()
            _ensure_inside(root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))

        commands = (
            ("harness-git-init", ["git", "init"]),
            ("harness-base-branch", ["git", "checkout", "-b", base]),
            ("harness-git-add", ["git", "add", "--force", "."]),
            ("harness-baseline-commit", ["git", "commit", "-m", "coding harness fixture baseline", "--allow-empty"]),
            ("harness-work-branch", ["git", "switch", "-c", branch]),
        )
        for label, argv in commands:
            result = _run_process(argv, cwd=repo_path)
            _append_command(task, result, label=label)
            if not result.get("ok"):
                task["status"] = "error"
                task["error"] = f"{label} failed"
                save_task(task)
                return public_task(task)
        head = _run_process(["git", "rev-parse", "HEAD"], cwd=repo_path)
        _append_command(task, head, label="harness-baseline-head")
        if not head.get("ok"):
            task["status"] = "error"
            task["error"] = "could not determine harness fixture baseline"
            save_task(task)
            return public_task(task)

        task["harness_baseline_commit"] = str(head.get("stdout") or "").strip()
        task["status"] = "ready"
        task.pop("error", None)
        save_task(task)
        return public_task(task)
    except Exception as exc:
        logger.warning("coding harness task create failed id=%s error=%s", task_id, exc)
        task["status"] = "error"
        task["error"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
        save_task(task)
        return public_task(task)


def create_model_integration_task(
    *,
    model: str,
    repo_url: Optional[str],
    preferred_runtime: Optional[str],
    route_kind: Optional[str],
    service_name: Optional[str],
    base_branch: Optional[str],
    branch_name: Optional[str],
    prompt: Optional[str],
    owner: Optional[str],
    owner_user_id: Optional[int] = None,
    git_token_value: Optional[str] = None,
    coding_model: Optional[str] = None,
    mission_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _ensure_enabled()
    _ensure_dirs()
    target_repo = _resolve_model_integration_repo_url(repo_url)
    try:
        plan = miw.build_integration_plan(
            model=model,
            preferred_runtime=preferred_runtime,
            route_kind=route_kind,
            service_name=service_name,
            prompt=prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"model integration plan failed: {type(exc).__name__}: {_redact_text(str(exc), extra_tokens=[_effective_git_token(git_token_value)])}") from exc
    task_id = new_task_id()
    branch = _safe_branch(branch_name, task_id=task_id)
    base = _base_branch(base_branch)
    workspace = _task_workspace(task_id)
    repo_path = _repo_path_for(task_id)
    task = {
        "schema": SCHEMA,
        "id": task_id,
        "kind": "model_integration",
        "status": "initializing",
        "created_at": _now(),
        "updated_at": _now(),
        "owner": owner or "unknown",
        "owner_user_id": owner_user_id,
        "repo_url": target_repo,
        "source_url": str(plan.get("source_url") or ""),
        "base_branch": base,
        "branch_name": branch,
        "prompt": str(plan.get("prompt") or "").strip(),
        "coding_model": str(coding_model or "").strip(),
        "workspace_path": str(workspace),
        "repo_path": str(repo_path),
        "integration": plan,
        "model_integration_dossier": plan.get("dossier") if isinstance(plan.get("dossier"), dict) else {},
        "commands": [],
        "project_plan": normalize_project_plan({"goal": str(plan.get("prompt") or "").strip(), "items": []}),
        "agent_runs": [],
    }
    task["mission"] = normalize_coding_mission(task, mission_overrides)
    save_task(task)

    try:
        workspace.mkdir(parents=True, exist_ok=True)
        remote_meta = _ensure_model_integration_remote(task, repo_url=target_repo, git_token_value=git_token_value)
        if remote_meta is None:
            save_task(task)
            return public_task(task)

        remote_is_empty = bool(remote_meta.get("empty"))
        if remote_is_empty:
            repo_path.mkdir(parents=True, exist_ok=True)
            init_result = _run_process(["git", "init"], cwd=repo_path, use_git_credentials=False)
            _append_command(task, init_result, label="git-init")
            if not init_result.get("ok"):
                task["status"] = "error"
                task["error"] = "git init failed"
                save_task(task)
                return public_task(task)

            task["seed_files"] = miw.scaffold_workspace(repo_path, plan)

            add_result = _run_process(["git", "add", "."], cwd=repo_path, use_git_credentials=False)
            _append_command(task, add_result, label="git-add")
            if not add_result.get("ok"):
                task["status"] = "error"
                task["error"] = "git add failed"
                save_task(task)
                return public_task(task)

            commit_result = _run_process(["git", "commit", "-m", "Seed model integration workspace"], cwd=repo_path, use_git_credentials=False)
            _append_command(task, commit_result, label="git-commit")
            if not commit_result.get("ok"):
                task["status"] = "error"
                task["error"] = "git commit failed"
                save_task(task)
                return public_task(task)

            rename_result = _run_process(["git", "branch", "-M", base], cwd=repo_path, use_git_credentials=False)
            _append_command(task, rename_result, label="git-branch-base")
            if not rename_result.get("ok"):
                task["status"] = "error"
                task["error"] = "base branch rename failed"
                save_task(task)
                return public_task(task)

            if branch != base:
                switch_result = _run_process(["git", "switch", "-c", branch], cwd=repo_path, use_git_credentials=False)
                if not switch_result.get("ok"):
                    switch_result = _run_process(["git", "checkout", "-b", branch], cwd=repo_path, use_git_credentials=False)
                _append_command(task, switch_result, label="git-branch-work")
                if not switch_result.get("ok"):
                    task["status"] = "error"
                    task["error"] = "working branch creation failed"
                    save_task(task)
                    return public_task(task)

            if not _attach_model_integration_remote(
                task,
                repo=repo_path,
                repo_url=target_repo,
                base_branch=base,
                branch_name=branch,
                git_token_value=git_token_value,
                remote_meta=remote_meta,
            ):
                save_task(task)
                return public_task(task)
        else:
            clone_result = _run_process(
                ["git", "clone", "--depth", "1", "--branch", base, target_repo, str(repo_path)],
                cwd=workspace,
                timeout_sec=max(command_timeout_sec(), 300.0),
                use_git_credentials=True,
                git_token_value=git_token_value,
            )
            _append_command(task, clone_result, label="git-clone-base")
            if not clone_result.get("ok"):
                task["status"] = "error"
                task["error"] = "git clone failed"
                save_task(task)
                return public_task(task)

            if branch != base:
                switch_result = _run_process(["git", "switch", "-c", branch], cwd=repo_path, use_git_credentials=False)
                if not switch_result.get("ok"):
                    switch_result = _run_process(["git", "checkout", "-b", branch], cwd=repo_path, use_git_credentials=False)
                _append_command(task, switch_result, label="git-branch-work")
                if not switch_result.get("ok"):
                    task["status"] = "error"
                    task["error"] = "working branch creation failed"
                    save_task(task)
                    return public_task(task)

            task["seed_files"] = miw.scaffold_workspace(repo_path, plan)

            add_result = _run_process(["git", "add", "."], cwd=repo_path, use_git_credentials=False)
            _append_command(task, add_result, label="git-add")
            if not add_result.get("ok"):
                task["status"] = "error"
                task["error"] = "git add failed"
                save_task(task)
                return public_task(task)

            commit_result = _run_process(["git", "commit", "-m", "Seed model integration workspace"], cwd=repo_path, use_git_credentials=False)
            _append_command(task, commit_result, label="git-commit")
            if not commit_result.get("ok"):
                task["status"] = "error"
                task["error"] = "git commit failed"
                save_task(task)
                return public_task(task)

            push_target = branch or base
            push_result = _run_process(
                ["git", "push", "-u", "origin", push_target],
                cwd=repo_path,
                timeout_sec=max(command_timeout_sec(), 300.0),
                use_git_credentials=True,
                git_token_value=git_token_value,
            )
            _append_command(task, push_result, label="git-push-branch" if push_target != base else "git-push-base")
            if not push_result.get("ok"):
                task["status"] = "error"
                task["error"] = "initial working branch push failed" if push_target != base else "initial base branch push failed"
                save_task(task)
                return public_task(task)
            task["last_pushed_at"] = _now()

        task["status"] = "ready"
        task.pop("error", None)
        save_task(task)
        return public_task(task)
    except Exception as exc:
        logger.warning("model integration task create failed id=%s error=%s", task_id, exc)
        task["status"] = "error"
        task["error"] = f"{type(exc).__name__}: {_redact_text(str(exc), extra_tokens=[_effective_git_token(git_token_value)])}"
        save_task(task)
        return public_task(task)


def _repo_path(task: Dict[str, Any]) -> Path:
    path = Path(str(task.get("repo_path") or "")).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="coding task workspace is missing")
    return path


def _ensure_inside(base: Path, target: Path) -> Path:
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    try:
        os.path.commonpath([str(base_resolved), str(target_resolved)])
    except Exception:
        raise HTTPException(status_code=400, detail="invalid path")
    if os.path.commonpath([str(base_resolved), str(target_resolved)]) != str(base_resolved):
        raise HTTPException(status_code=403, detail="path escapes task workspace")
    return target_resolved


def _resolve_repo_child(
    task: Dict[str, Any],
    rel_path: Optional[str] = None,
    *,
    repo_override: Optional[Path] = None,
) -> Path:
    base = repo_override.resolve() if repo_override is not None else _repo_path(task)
    if not base.is_dir() or base.is_symlink():
        raise HTTPException(status_code=409, detail="coding task workspace is missing")
    rel = str(rel_path or "").strip().lstrip("/\\")
    target = base.joinpath(rel) if rel else base
    resolved = _ensure_inside(base, target)
    parts = {part for part in Path(rel).parts}
    if ".git" in parts:
        raise HTTPException(status_code=403, detail=".git internals are not exposed through the coding file API")
    return resolved


def _validate_repo_relative_path(path: str) -> str:
    rel = str(path or "").strip().replace("\\", "/")
    if rel.startswith("a/") or rel.startswith("b/"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel or rel == "/dev/null":
        return ""
    if "\x00" in rel or re.match(r"^[A-Za-z]:/", rel):
        raise HTTPException(status_code=400, detail="invalid patch path")
    parts = [part for part in rel.split("/") if part]
    if any(part in {"..", ".git"} for part in parts):
        raise HTTPException(status_code=403, detail="patch path escapes the repository or targets .git")
    return "/".join(parts)


def _patch_paths(patch: str) -> List[str]:
    paths: List[str] = []
    for raw in str(patch or "").splitlines():
        line = raw.strip()
        if line.startswith("diff --git "):
            bits = line.split()
            if len(bits) >= 4:
                for item in bits[2:4]:
                    rel = _validate_repo_relative_path(item)
                    if rel:
                        paths.append(rel)
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            item = line[4:].strip().split("\t", 1)[0]
            if item == "/dev/null":
                continue
            if item.startswith('"') and item.endswith('"'):
                item = item[1:-1]
            rel = _validate_repo_relative_path(item)
            if rel:
                paths.append(rel)
    deduped: List[str] = []
    seen = set()
    for item in paths:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _parse_git_subcommand(argv: Sequence[str]) -> str:
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


def validate_command(argv: Sequence[str]) -> List[str]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise HTTPException(status_code=400, detail="argv must be a non-empty list")
    if len(argv) > 64:
        raise HTTPException(status_code=400, detail="argv is too long")
    out = []
    for item in argv:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail="argv entries must be strings")
        value = item
        if not value:
            raise HTTPException(status_code=400, detail="argv entries must be non-empty")
        if "\x00" in value:
            raise HTTPException(status_code=400, detail="argv entries cannot contain NUL")
        if len(value) > 4096:
            raise HTTPException(status_code=400, detail="argv entry is too long")
        out.append(value)
    if out[0] != out[0].strip():
        raise HTTPException(
            status_code=400,
            detail="command name cannot have leading or trailing whitespace",
        )
    cmd = Path(out[0]).name.lower()
    if out[0] != Path(out[0]).name:
        raise HTTPException(status_code=403, detail="commands must be invoked by name, not path")
    allowed = set(allowed_commands())
    if cmd not in allowed:
        raise HTTPException(status_code=403, detail=f"command not allowed: {cmd}")
    if cmd == "git":
        sub = _parse_git_subcommand(out)
        if not sub:
            raise HTTPException(status_code=400, detail="git subcommand required")
        if sub in _BLOCKED_GIT_SUBCOMMANDS:
            raise HTTPException(status_code=403, detail=f"git {sub} is blocked in coding workspaces")
    blocked_reason = _dependency_mutation_block_reason(out)
    if blocked_reason:
        raise HTTPException(status_code=403, detail=blocked_reason)
    return out


def _dependency_mutation_block_reason(argv: Sequence[str]) -> str:
    parts = [str(item).strip() for item in argv if str(item).strip()]
    if not parts:
        return ""
    cmd = Path(parts[0]).name.lower()
    lowered = [item.lower() for item in parts]
    meaningful = [item for item in lowered[1:] if not item.startswith("-")]

    if cmd == "npm" and meaningful:
        if meaningful[0] in {"install", "i", "add", "ci", "update", "upgrade", "remove", "rm", "uninstall", "dedupe", "rebuild", "link"}:
            return "dependency installation or mutation commands are blocked in coding workspaces"

    if cmd in {"python", "python3"} and "-m" in lowered:
        index = lowered.index("-m")
        module = lowered[index + 1] if index + 1 < len(lowered) else ""
        root_module = module.split(".", 1)[0]
        sub_meaningful = [item for item in lowered[index + 2 :] if not item.startswith("-")]
        if root_module in {"pip", "pip3"} and sub_meaningful:
            if sub_meaningful[0] in {"install", "uninstall", "download", "wheel"}:
                return "dependency installation or mutation commands are blocked in coding workspaces"

    if cmd == "uv" and meaningful:
        first = meaningful[0]
        second = meaningful[1] if len(meaningful) > 1 else ""
        if first in {"add", "remove", "sync", "lock", "venv"}:
            return "dependency installation or mutation commands are blocked in coding workspaces"
        if first == "pip" and second in {"install", "sync", "uninstall"}:
            return "dependency installation or mutation commands are blocked in coding workspaces"

    return ""


def run_task_command(
    task_id: str,
    *,
    argv: Sequence[str],
    cwd: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        task = load_task(task_id)
        repo = _repo_path(task)
        command = validate_command(argv)
        run_cwd = _resolve_repo_child(task, cwd) if cwd else repo
        if not run_cwd.exists() or not run_cwd.is_dir():
            raise HTTPException(status_code=400, detail="cwd must be an existing directory inside the task repo")
        cmd = Path(command[0]).name.lower()
        result = _run_process(
            command,
            cwd=run_cwd,
            timeout_sec=timeout_sec,
            use_git_credentials=cmd in {"git", "gh"},
            git_token_value=git_token_value,
        )
        mutate_task(
            task_id,
            lambda current: _append_command(current, result, label="command"),
        )
        return result


def run_harness_validation_command(
    task_id: str,
    *,
    argv: Sequence[str],
    cwd: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    evidence_lease_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run trusted fixture validation with the harness run budget as its timeout cap."""
    evidence_workspace: Optional[Path] = None
    baseline_bytes: Optional[int] = None
    baseline_entries: Optional[int] = None
    with _HARNESS_VALIDATIONS_GUARD:
        active_lease = _active_harness_evidence_lease_locked(task_id)
        provided_lease = str(evidence_lease_id or "")
        if active_lease != provided_lease and (active_lease or provided_lease):
            raise HTTPException(status_code=409, detail="coding harness evidence lease is not active")
        if provided_lease:
            evidence_workspace, baseline_bytes, baseline_entries = (
                _harness_evidence_workspace_locked(task_id, provided_lease)
            )
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is already active")
        if task_id in _ACTIVE_HARNESS_RUN_STARTS:
            raise HTTPException(status_code=409, detail="coding harness agent run is starting")
        if _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness agent tool is still active")
        if _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness evidence read is still active")
        _ACTIVE_HARNESS_VALIDATIONS.add(task_id)
    try:
        task = load_task(task_id)
        if str(task.get("kind") or "") != "harness_eval":
            raise HTTPException(status_code=403, detail="harness validation requires a harness task")
        agent_status = str(task.get("agent_status") or "idle").strip().lower()
        if agent_status in {"queued", "running", "stopping", "pausing"}:
            raise HTTPException(status_code=409, detail="coding harness task is still active")
        repo = _repo_path(task)
        command = validate_command(argv)
        validation_repo = evidence_workspace or repo
        run_cwd = (
            _resolve_repo_child(
                task,
                cwd,
                repo_override=evidence_workspace,
            )
            if cwd
            else validation_repo
        )
        if not run_cwd.exists() or not run_cwd.is_dir():
            raise HTTPException(status_code=400, detail="cwd must be an existing directory inside the task repo")
        mission = normalize_coding_mission(task)
        timeout_limit = max(
            1.0,
            min(float(mission["budget_policy"]["max_runtime_sec"]), 3600.0),
        )
        result = _run_process(
            command,
            cwd=run_cwd,
            timeout_sec=timeout_sec,
            timeout_limit_sec=timeout_limit,
            use_git_credentials=False,
            isolate_process_group=True,
            validation_workspace=evidence_workspace,
            validation_baseline_bytes=baseline_bytes,
            validation_baseline_entries=baseline_entries,
        )
        mutate_task(
            task_id,
            lambda current: _append_command(
                current,
                result,
                label="harness-validation",
            ),
        )
        return result
    finally:
        with _HARNESS_VALIDATIONS_GUARD:
            _ACTIVE_HARNESS_VALIDATIONS.discard(task_id)


def begin_harness_agent_tool(task_id: str) -> bool:
    """Register a harness tool worker before it can mutate task state."""
    with _HARNESS_VALIDATIONS_GUARD:
        if _active_harness_evidence_lease_locked(task_id):
            raise HTTPException(status_code=409, detail="coding harness evidence lease is active")
        task = load_task(task_id)
        if str(task.get("kind") or "") != "harness_eval":
            return False
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is still active")
        if task_id in _ACTIVE_HARNESS_RUN_STARTS:
            raise HTTPException(status_code=409, detail="coding harness agent run is starting")
        if _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness evidence read is still active")
        _ACTIVE_HARNESS_AGENT_TOOLS[task_id] = (
            _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) + 1
        )
        return True


def end_harness_agent_tool(task_id: str, *, registered: bool) -> None:
    if not registered:
        return
    with _HARNESS_VALIDATIONS_GUARD:
        remaining = _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) - 1
        if remaining > 0:
            _ACTIVE_HARNESS_AGENT_TOOLS[task_id] = remaining
        else:
            _ACTIVE_HARNESS_AGENT_TOOLS.pop(task_id, None)


@contextmanager
def _harness_mutation_guard(task_id: str):
    """Serialize generic workspace mutations with harness evidence leases."""
    registered = begin_harness_agent_tool(task_id)
    try:
        yield
    finally:
        end_harness_agent_tool(task_id, registered=registered)


def begin_harness_evidence_read(
    task_id: str,
    *,
    evidence_lease_id: Optional[str] = None,
) -> Optional[Path]:
    with _HARNESS_VALIDATIONS_GUARD:
        active_lease = _active_harness_evidence_lease_locked(task_id)
        provided_lease = str(evidence_lease_id or "")
        if active_lease != provided_lease and (active_lease or provided_lease):
            raise HTTPException(status_code=409, detail="coding harness evidence lease is not active")
        workspace = None
        if provided_lease:
            workspace, _, _ = _harness_evidence_workspace_locked(
                task_id,
                provided_lease,
            )
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is still active")
        if task_id in _ACTIVE_HARNESS_RUN_STARTS:
            raise HTTPException(status_code=409, detail="coding harness agent run is starting")
        if _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness agent tool is still active")
        _ACTIVE_HARNESS_EVIDENCE_READS[task_id] = (
            _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) + 1
        )
        return workspace


def end_harness_evidence_read(task_id: str) -> None:
    with _HARNESS_VALIDATIONS_GUARD:
        remaining = _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) - 1
        if remaining > 0:
            _ACTIVE_HARNESS_EVIDENCE_READS[task_id] = remaining
        else:
            _ACTIVE_HARNESS_EVIDENCE_READS.pop(task_id, None)


def _cleanup_harness_evidence_lease(record: Dict[str, Any]) -> None:
    expiry_timer = record.get("expiry_timer")
    if isinstance(expiry_timer, threading.Timer):
        expiry_timer.cancel()
    temporary = record.get("temporary")
    if not isinstance(temporary, tempfile.TemporaryDirectory):
        return
    try:
        temporary.cleanup()
    except OSError as exc:
        logger.warning("could not clean harness evidence workspace: %s", exc)


def _schedule_harness_evidence_expiry_locked(
    lease_id: str,
    record: Dict[str, Any],
    *,
    delay_sec: float,
) -> None:
    expires_at = float(record.get("expires_at") or 0)
    timer = threading.Timer(
        max(0.01, float(delay_sec)),
        _expire_harness_evidence_lease,
        args=(lease_id, expires_at),
    )
    timer.daemon = True
    record["expiry_timer"] = timer
    timer.start()


def _expire_harness_evidence_lease(lease_id: str, expected_expires_at: float) -> None:
    with _HARNESS_VALIDATIONS_GUARD:
        record = _ACTIVE_HARNESS_EVIDENCE_LEASES.get(lease_id)
        if (
            not isinstance(record, dict)
            or float(record.get("expires_at") or 0) != expected_expires_at
        ):
            return
        remaining = expected_expires_at - _now()
        if remaining > 0:
            _schedule_harness_evidence_expiry_locked(
                lease_id,
                record,
                delay_sec=remaining,
            )
            return
        task_id = str(record.get("task_id") or "")
        if (
            task_id in _ACTIVE_HARNESS_VALIDATIONS
            or _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0
        ):
            _schedule_harness_evidence_expiry_locked(
                lease_id,
                record,
                delay_sec=0.25,
            )
            return
        removed = _ACTIVE_HARNESS_EVIDENCE_LEASES.pop(lease_id, None)
        if isinstance(removed, dict):
            _cleanup_harness_evidence_lease(removed)


def _expire_harness_evidence_leases_locked() -> None:
    current = _now()
    expired = [
        lease_id
        for lease_id, record in _ACTIVE_HARNESS_EVIDENCE_LEASES.items()
        if float(record.get("expires_at") or 0) <= current
    ]
    for lease_id in expired:
        record = _ACTIVE_HARNESS_EVIDENCE_LEASES.get(lease_id)
        task_id = str(record.get("task_id") or "") if isinstance(record, dict) else ""
        if (
            task_id in _ACTIVE_HARNESS_VALIDATIONS
            or _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0
        ):
            continue
        removed = _ACTIVE_HARNESS_EVIDENCE_LEASES.pop(lease_id, None)
        if isinstance(removed, dict):
            _cleanup_harness_evidence_lease(removed)


def _active_harness_evidence_lease_locked(task_id: str) -> str:
    _expire_harness_evidence_leases_locked()
    for lease_id, record in _ACTIVE_HARNESS_EVIDENCE_LEASES.items():
        if str(record.get("task_id") or "") == task_id:
            return lease_id
    return ""


def _harness_evidence_workspace_locked(
    task_id: str,
    lease_id: str,
) -> tuple[Path, int, int]:
    record = _ACTIVE_HARNESS_EVIDENCE_LEASES.get(lease_id)
    if not isinstance(record, dict) or str(record.get("task_id") or "") != task_id:
        raise HTTPException(status_code=409, detail="coding harness evidence lease is not active")
    workspace = Path(str(record.get("workspace") or "")).resolve()
    temporary = record.get("temporary")
    if (
        not isinstance(temporary, tempfile.TemporaryDirectory)
        or not workspace.is_dir()
        or workspace.is_symlink()
        or workspace.parent != Path(temporary.name).resolve()
    ):
        raise HTTPException(status_code=409, detail="coding harness evidence workspace is unavailable")
    return (
        workspace,
        max(0, int(record.get("baseline_bytes") or 0)),
        max(0, int(record.get("baseline_entries") or 0)),
    )


def acquire_harness_evidence_lease(
    task_id: str,
    *,
    ttl_sec: float,
) -> Dict[str, Any]:
    """Block harness mutation while a client collects multi-request evidence."""
    with _HARNESS_VALIDATIONS_GUARD:
        task = load_task(task_id)
        if str(task.get("kind") or "") != "harness_eval":
            raise HTTPException(status_code=403, detail="evidence lease requires a harness task")
        if str(task.get("status") or "").strip().lower() == "initializing":
            raise HTTPException(status_code=409, detail="coding harness task is still initializing")
        if _active_harness_evidence_lease_locked(task_id):
            raise HTTPException(status_code=409, detail="coding harness evidence lease is already active")
        if task_id in _ACTIVE_HARNESS_RUN_STARTS:
            raise HTTPException(status_code=409, detail="coding harness agent run is starting")
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is still active")
        if _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness agent tool is still active")
        if _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness evidence read is still active")
        agent_status = str(task.get("agent_status") or "idle").strip().lower()
        if agent_status in {"queued", "running", "stopping", "pausing"}:
            raise HTTPException(status_code=409, detail="coding harness task is still active")
        duration = max(30.0, min(float(ttl_sec), _HARNESS_EVIDENCE_LEASE_MAX_SEC))
        lease_id = secrets.token_urlsafe(24)
        expires_at = _now() + duration
        temporary, _, workspace, baseline_bytes, baseline_entries = (
            _create_staged_validation_tree(_repo_path(task))
        )
        _ACTIVE_HARNESS_EVIDENCE_LEASES[lease_id] = {
            "task_id": task_id,
            "expires_at": expires_at,
            "temporary": temporary,
            "workspace": str(workspace),
            "baseline_bytes": baseline_bytes,
            "baseline_entries": baseline_entries,
        }
        _schedule_harness_evidence_expiry_locked(
            lease_id,
            _ACTIVE_HARNESS_EVIDENCE_LEASES[lease_id],
            delay_sec=duration,
        )
        return {"lease_id": lease_id, "task_id": task_id, "expires_at": expires_at}


def release_harness_evidence_lease(task_id: str, *, lease_id: str) -> Dict[str, Any]:
    with _HARNESS_VALIDATIONS_GUARD:
        _expire_harness_evidence_leases_locked()
        record = _ACTIVE_HARNESS_EVIDENCE_LEASES.get(str(lease_id or ""))
        if not isinstance(record, dict) or str(record.get("task_id") or "") != task_id:
            raise HTTPException(status_code=404, detail="coding harness evidence lease was not found")
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is still active")
        if _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness evidence read is still active")
        removed = _ACTIVE_HARNESS_EVIDENCE_LEASES.pop(lease_id)
        _cleanup_harness_evidence_lease(removed)
        return {"ok": True, "task_id": task_id, "lease_id": lease_id}


def begin_harness_agent_run_start(task_id: str) -> bool:
    """Serialize a harness run transition with evidence, validation, and deletion."""
    with _HARNESS_VALIDATIONS_GUARD:
        if _active_harness_evidence_lease_locked(task_id):
            raise HTTPException(status_code=409, detail="coding harness evidence lease is active")
        task = load_task(task_id)
        if str(task.get("kind") or "") != "harness_eval":
            return False
        if str(task.get("status") or "").strip().lower() == "initializing":
            raise HTTPException(status_code=409, detail="coding harness task is still initializing")
        if task_id in _ACTIVE_HARNESS_RUN_STARTS:
            raise HTTPException(status_code=409, detail="coding harness agent run is already starting")
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is still active")
        if _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness agent tool is still active")
        if _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness evidence read is still active")
        _ACTIVE_HARNESS_RUN_STARTS.add(task_id)
        return True


def end_harness_agent_run_start(task_id: str, *, registered: bool) -> None:
    if not registered:
        return
    with _HARNESS_VALIDATIONS_GUARD:
        _ACTIVE_HARNESS_RUN_STARTS.discard(task_id)


def git_status(task_id: str, *, git_token_value: Optional[str] = None) -> Dict[str, Any]:
    result = run_task_command(task_id, argv=["git", "status", "--short", "--branch"], git_token_value=git_token_value)
    return result


def git_head(task_id: str) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    result = _run_process(["git", "rev-parse", "HEAD"], cwd=repo)
    return {"ok": bool(result.get("ok")), "commit": str(result.get("stdout") or "").strip(), "raw": result}


def workspace_progress_fingerprint(task_id: str) -> str:
    """Hash controller-owned repository state without mutating task history."""
    task = load_task(task_id)
    repo = _repo_path(task)
    head = _run_process(["git", "rev-parse", "HEAD"], cwd=repo)
    status = _run_process(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
    )
    diff = _run_process(["git", "diff", "--binary", "HEAD", "--"], cwd=repo)
    digest = hashlib.sha256()
    for result in (head, status, diff):
        digest.update(str(result.get("returncode")).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(result.get("stdout") or "").encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(bool(result.get("stdout_truncated"))).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def git_change_summary(
    task_id: str,
    *,
    limit: int = 500,
    nul_paths: bool = False,
) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    argv = ["git", "status", "--porcelain=v1", "--untracked-files=all"]
    if nul_paths:
        argv.append("-z")
    result = _run_process(argv, cwd=repo)
    counts = {"added": 0, "modified": 0, "removed": 0, "renamed": 0, "untracked": 0, "other": 0, "total": 0}
    files: List[Dict[str, Any]] = []
    if result.get("ok"):
        raw_output = str(result.get("stdout") or "")
        records: List[Tuple[str, str, Optional[str]]] = []
        if nul_paths:
            tokens = raw_output.split("\0")
            if tokens and tokens[-1] == "":
                tokens.pop()
            index = 0
            while index < len(tokens):
                record = tokens[index]
                index += 1
                code = record[:2]
                path = record[3:] if len(record) > 3 else ""
                previous_path = None
                if any(marker in code for marker in ("R", "C")) and index < len(tokens):
                    previous_path = tokens[index]
                    index += 1
                records.append((code, path, previous_path))
        else:
            for raw_line in raw_output.splitlines():
                line = raw_line.rstrip()
                if line:
                    records.append((line[:2], line[3:] if len(line) > 3 else "", None))
        for code, path, previous_path in records:
            x = code[0] if len(code) > 0 else " "
            y = code[1] if len(code) > 1 else " "
            if code == "??":
                kind = "untracked"
                counts["untracked"] += 1
                counts["added"] += 1
            elif "D" in {x, y}:
                kind = "removed"
                counts["removed"] += 1
            elif "R" in {x, y}:
                kind = "renamed"
                counts["renamed"] += 1
                counts["modified"] += 1
            elif "A" in {x, y}:
                kind = "added"
                counts["added"] += 1
            elif any(item in {x, y} for item in ["M", "T", "U"]):
                kind = "modified"
                counts["modified"] += 1
            else:
                kind = "other"
                counts["other"] += 1
            counts["total"] += 1
            item = {"path": path, "status": code, "kind": kind}
            if previous_path is not None:
                item["previous_path"] = previous_path
            files.append(item)
    max_files = max(1, min(int(limit or 500), 2000))
    return {
        "ok": bool(result.get("ok")),
        "counts": counts,
        "files": files[:max_files],
        "truncated": bool(result.get("stdout_truncated") or len(files) > max_files),
        "raw": result,
    }


def _diff_kind_from_status(status: str) -> str:
    code = str(status or "").strip().upper()
    head = code[:1]
    if code == "??":
        return "untracked"
    if head == "A":
        return "added"
    if head == "D":
        return "removed"
    if head == "R":
        return "renamed"
    if head in {"M", "T", "U", "C"}:
        return "modified"
    return "other"


def _counts_for_change_files(files: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"added": 0, "modified": 0, "removed": 0, "renamed": 0, "untracked": 0, "other": 0, "total": 0}
    for item in files:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "other")
        if kind not in counts:
            kind = "other"
        counts[kind] += 1
        counts["total"] += 1
    return counts


def _git_name_status_summary(
    repo: Path,
    argv: Sequence[str],
    *,
    limit: int = 500,
    env_overrides: Optional[Dict[str, str]] = None,
    output_limit_chars: Optional[int] = None,
) -> Dict[str, Any]:
    result = _run_process(
        list(argv),
        cwd=repo,
        env_overrides=env_overrides,
        output_limit_chars=output_limit_chars,
    )
    files: List[Dict[str, Any]] = []
    if result.get("ok"):
        raw_output = str(result.get("stdout") or "")
        parsed: List[Tuple[str, str, Optional[str]]] = []
        if "-z" in argv:
            tokens = raw_output.split("\0")
            if tokens and tokens[-1] == "":
                tokens.pop()
            index = 0
            while index < len(tokens):
                status = tokens[index].strip()
                index += 1
                kind = _diff_kind_from_status(status)
                if kind == "renamed" or status[:1].upper() == "C":
                    previous_path = tokens[index] if index < len(tokens) else None
                    index += 1
                    path = tokens[index] if index < len(tokens) else ""
                    index += 1
                else:
                    previous_path = None
                    path = tokens[index] if index < len(tokens) else ""
                    index += 1
                parsed.append((status, path, previous_path))
        else:
            for raw_line in raw_output.splitlines():
                line = raw_line.rstrip()
                if not line:
                    continue
                parts = line.split("\t")
                status = str(parts[0] if parts else "").strip()
                path = str(parts[-1] if len(parts) >= 2 else "").strip()
                previous_path = str(parts[1] if len(parts) >= 3 else "").strip() or None
                parsed.append((status, path, previous_path))
        for status, path, previous_path in parsed:
            kind = _diff_kind_from_status(status)
            files.append(
                {
                    "path": path,
                    "previous_path": previous_path,
                    "status": status,
                    "kind": kind,
                }
            )
    max_files = max(1, min(int(limit or 500), 2000))
    return {
        "ok": bool(result.get("ok")),
        "counts": _counts_for_change_files(files),
        "files": files[:max_files],
        "truncated": bool(result.get("stdout_truncated") or len(files) > max_files),
        "raw": result,
    }


def _git_ref_exists(repo: Path, ref: str) -> bool:
    candidate = str(ref or "").strip()
    if not candidate:
        return False
    result = _run_process(["git", "rev-parse", "--verify", candidate], cwd=repo)
    return bool(result.get("ok"))


def _git_base_branch_diff(
    repo: Path,
    *,
    base_branch: str,
    change_limit: int = 500,
    nul_paths: bool = False,
) -> Dict[str, Any]:
    base = _base_branch(base_branch)
    base_ref = ""
    for candidate in (f"origin/{base}", base):
        if _git_ref_exists(repo, candidate):
            base_ref = candidate
            break
    if not base_ref:
        return {
            "ok": False,
            "scope": "base_branch",
            "base_branch": base,
            "error": f"base branch ref not found: {base}",
        }

    merge_base_result = _run_process(["git", "merge-base", "HEAD", base_ref], cwd=repo)
    merge_base = str(merge_base_result.get("stdout") or "").strip() if merge_base_result.get("ok") else ""
    compare_ref = merge_base or base_ref
    stat = _run_process(["git", "diff", "--stat", compare_ref, "--"], cwd=repo)
    diff = _run_process(["git", "diff", compare_ref, "--"], cwd=repo)
    workspace_changes = _git_name_status_summary(
        repo,
        ["git", "diff", "--name-status", *(["-z"] if nul_paths else []), compare_ref, "--"],
        limit=change_limit,
    )
    committed_stat = _run_process(["git", "diff", "--stat", compare_ref, "HEAD", "--"], cwd=repo)
    committed_diff = _run_process(["git", "diff", compare_ref, "HEAD", "--"], cwd=repo)
    committed_changes = _git_name_status_summary(
        repo,
        [
            "git",
            "diff",
            "--name-status",
            *(["-z"] if nul_paths else []),
            compare_ref,
            "HEAD",
            "--",
        ],
        limit=change_limit,
    )
    return {
        "ok": bool(
            stat.get("ok")
            and diff.get("ok")
            and workspace_changes.get("ok")
            and committed_stat.get("ok")
            and committed_diff.get("ok")
            and committed_changes.get("ok")
        ),
        "scope": "base_branch",
        "base_branch": base,
        "base_ref": base_ref,
        "merge_base": merge_base,
        "compare_ref": compare_ref,
        "stat": stat,
        "diff": diff,
        "changes": workspace_changes,
        "committed_stat": committed_stat,
        "committed_diff": committed_diff,
        "committed_changes": committed_changes,
        "merge_base_result": merge_base_result,
    }


def search_text(
    task_id: str,
    *,
    query: str,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    fixed_strings: bool = False,
    case_sensitive: bool = True,
    limit: int = 200,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _search_text(
            task_id,
            query=query,
            path=path,
            glob=glob,
            fixed_strings=fixed_strings,
            case_sensitive=case_sensitive,
            limit=limit,
        )


def _search_text(
    task_id: str,
    *,
    query: str,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    fixed_strings: bool = False,
    case_sensitive: bool = True,
    limit: int = 200,
) -> Dict[str, Any]:
    needle = str(query or "").strip()
    if not needle:
        raise HTTPException(status_code=400, detail="query is required")
    if len(needle) > 500:
        raise HTTPException(status_code=400, detail="query is too long")
    task = load_task(task_id)
    repo = _repo_path(task)
    max_matches = max(1, min(int(limit or 200), 1000))
    argv = ["rg", "-n", "--column", "--hidden", "--glob", "!.git"]
    if fixed_strings:
        argv.append("-F")
    if not case_sensitive:
        argv.append("-i")
    glob_value = str(glob or "").strip()
    if glob_value:
        argv.extend(["--glob", glob_value])
    argv.extend(["--", needle])
    rel_path = str(path or "").strip().lstrip("/\\")
    if rel_path:
        target = _resolve_repo_child(task, rel_path)
        if not target.exists():
            raise HTTPException(status_code=404, detail="search path not found")
        argv.append(rel_path)
    result = _run_process(argv, cwd=repo)
    _append_command(task, result, label="search")
    save_task(task)
    matches: List[Dict[str, Any]] = []
    for raw_line in str(result.get("stdout") or "").splitlines():
        if len(matches) >= max_matches:
            break
        parts = raw_line.split(":", 3)
        if len(parts) < 4:
            continue
        match_path, line_raw, col_raw, text = parts
        try:
            line_no = int(line_raw)
        except Exception:
            line_no = 0
        try:
            column = int(col_raw)
        except Exception:
            column = 0
        matches.append({"path": match_path, "line": line_no, "column": column, "text": text})
    return {
        "ok": result.get("returncode") in {0, 1},
        "matched": int(result.get("returncode") or 0) == 0,
        "query": needle,
        "matches": matches,
        "truncated": len(matches) >= max_matches,
        "raw": result,
    }


def apply_unified_patch(
    task_id: str,
    *,
    patch: str,
    check_only: bool = False,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _apply_unified_patch(task_id, patch=patch, check_only=check_only)


def _apply_unified_patch(task_id: str, *, patch: str, check_only: bool = False) -> Dict[str, Any]:
    raw = str(patch or "")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="patch is required")
    if len(raw.encode("utf-8")) > file_max_bytes():
        raise HTTPException(status_code=413, detail="patch is too large")
    paths = _patch_paths(raw)
    if not paths:
        raise HTTPException(status_code=400, detail="patch must include repository-relative file paths")
    task = load_task(task_id)
    repo = _repo_path(task)
    for rel in paths:
        _resolve_repo_child(task, rel)
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, prefix="nexus-coding-", suffix=".patch") as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        check = _run_process(["git", "apply", "--check", "--whitespace=nowarn", tmp_path], cwd=repo)
        _append_command(task, check, label="patch-check")
        if check_only or not check.get("ok"):
            save_task(task)
            return {
                "ok": bool(check.get("ok")),
                "check_only": bool(check_only),
                "paths": paths,
                "check": check,
                "error": "" if check.get("ok") else "patch check failed",
            }
        apply = _run_process(["git", "apply", "--whitespace=nowarn", tmp_path], cwd=repo)
        _append_command(task, apply, label="patch-apply")
        if apply.get("ok"):
            task["last_file_write_at"] = _now()
        save_task(task)
        return {"ok": bool(apply.get("ok")), "paths": paths, "check": check, "apply": apply}
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except FileNotFoundError:
                pass


def git_diff(
    task_id: str,
    *,
    change_limit: int = 500,
    nul_paths: bool = False,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _git_diff(
            task_id,
            change_limit=change_limit,
            nul_paths=nul_paths,
        )


def _git_diff(
    task_id: str,
    *,
    change_limit: int = 500,
    nul_paths: bool = False,
) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    branch = str(task.get("branch_name") or "").strip()
    base_branch = str(task.get("base_branch") or "main").strip()
    base_diff = _git_base_branch_diff(
        repo,
        base_branch=base_branch,
        change_limit=change_limit,
        nul_paths=nul_paths,
    )
    worktree_stat = _run_process(["git", "diff", "--stat"], cwd=repo)
    worktree_diff = _run_process(["git", "diff", "--"], cwd=repo)
    staged_stat = _run_process(["git", "diff", "--cached", "--stat"], cwd=repo)
    staged_diff = _run_process(["git", "diff", "--cached", "--"], cwd=repo)
    result = {
        "ok": bool(base_diff.get("ok") and worktree_diff.get("ok") and worktree_stat.get("ok") and staged_diff.get("ok") and staged_stat.get("ok")),
        "scope": str(base_diff.get("scope") or "base_branch"),
        "base_branch": base_branch,
        "branch_name": branch,
        "base_ref": str(base_diff.get("base_ref") or ""),
        "merge_base": str(base_diff.get("merge_base") or ""),
        "compare_ref": str(base_diff.get("compare_ref") or ""),
        "stat": base_diff.get("stat") or {"ok": False, "stdout": "", "stderr": ""},
        "diff": base_diff.get("diff") or {"ok": False, "stdout": "", "stderr": ""},
        "changes": base_diff.get("changes") or {"ok": False, "counts": {"total": 0}, "files": []},
        "committed_stat": base_diff.get("committed_stat") or {"ok": False, "stdout": "", "stderr": ""},
        "committed_diff": base_diff.get("committed_diff") or {"ok": False, "stdout": "", "stderr": ""},
        "committed_changes": base_diff.get("committed_changes") or {"ok": False, "counts": {"total": 0}, "files": []},
        "worktree_stat": worktree_stat,
        "worktree_diff": worktree_diff,
        "staged_stat": staged_stat,
        "staged_diff": staged_diff,
    }
    if base_diff.get("error"):
        result["error"] = str(base_diff.get("error"))
    _append_command(
        task,
        {
            "ok": result["ok"],
            "returncode": 0 if result["ok"] else 1,
            "argv": ["git", "diff", str(result.get("compare_ref") or base_branch), "--"],
            "stdout": str((result.get("diff") or {}).get("stdout") or ""),
            "stderr": str((result.get("diff") or {}).get("stderr") or result.get("error") or ""),
            "duration_ms": 0,
        },
        label="diff",
    )
    save_task(task)
    return result


def _harness_neutral_git_snapshot(
    task: Dict[str, Any],
    *,
    change_limit: int,
    repo_override: Optional[Path] = None,
) -> Dict[str, Any]:
    """Collect fixture evidence without trusting the agent-mutated index or Git config."""
    repo = repo_override.resolve() if repo_override is not None else _repo_path(task)
    if not repo.is_dir() or repo.is_symlink():
        raise HTTPException(status_code=409, detail="harness evidence workspace is unavailable")
    source_git_dir = repo / ".git"
    source_objects = source_git_dir / "objects"
    baseline = str(task.get("harness_baseline_commit") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", baseline):
        raise HTTPException(status_code=409, detail="harness baseline commit is unavailable")
    if (
        not source_git_dir.is_dir()
        or source_git_dir.is_symlink()
        or not source_objects.is_dir()
        or source_objects.is_symlink()
    ):
        raise HTTPException(status_code=409, detail="harness Git metadata is unavailable")

    max_files = max(1, min(int(change_limit or 500), 2000))
    with tempfile.TemporaryDirectory(prefix="nexus-harness-git-") as temp_root_raw:
        temp_root = Path(temp_root_raw)
        neutral_git_dir = temp_root / "git"
        home_dir = temp_root / "home"
        home_dir.mkdir(mode=0o700)
        initialized = _run_process(
            ["git", "init", "--bare", str(neutral_git_dir)],
            cwd=repo,
            use_git_credentials=False,
        )
        if not initialized.get("ok"):
            return {
                "diff": {"ok": False, "error": "could not initialize neutral Git evidence"},
                "changes": {"ok": False, "files": [], "counts": _counts_for_change_files([])},
            }
        (neutral_git_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = true\n",
            encoding="utf-8",
        )
        info_dir = neutral_git_dir / "info"
        info_dir.mkdir(mode=0o700, exist_ok=True)
        (info_dir / "attributes").write_text(
            "* -text -crlf -ident -filter !working-tree-encoding !diff\n",
            encoding="utf-8",
        )
        (info_dir / "exclude").write_text("/.git/\n", encoding="utf-8")
        env = {
            "GIT_DIR": str(neutral_git_dir),
            "GIT_INDEX_FILE": str(temp_root / "index"),
            "GIT_WORK_TREE": str(repo),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(source_objects),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "HOME": str(home_dir),
        }
        read_tree = _run_process(
            ["git", "read-tree", baseline],
            cwd=repo,
            use_git_credentials=False,
            env_overrides=env,
        )
        if not read_tree.get("ok"):
            error = str(read_tree.get("stderr") or "could not load harness baseline")
            return {
                "diff": {"ok": False, "error": error},
                "changes": {"ok": False, "files": [], "counts": _counts_for_change_files([])},
            }

        untracked_result = _run_process(
            [
                "git",
                "ls-files",
                "-z",
                "--others",
                "--exclude=.git",
                "--exclude=.git/**",
            ],
            cwd=repo,
            env_overrides=env,
            decode_errors="surrogateescape",
            output_limit_chars=_HARNESS_MAX_DIFF_CHARS,
        )
        untracked_stdout = str(untracked_result.get("stdout") or "")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in untracked_stdout):
            error = "harness repository contains a path that is not valid UTF-8"
            empty_changes = {
                "ok": False,
                "error": error,
                "files": [],
                "counts": _counts_for_change_files([]),
            }
            return {
                "diff": {"ok": False, "error": error, "changes": empty_changes},
                "changes": empty_changes,
            }
        untracked_paths = [
            path
            for path in untracked_stdout.split("\0")
            if path and path != ".git" and not path.startswith(".git/")
        ]
        diff_argv = [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
        ]
        tracked = _git_name_status_summary(
            repo,
            [*diff_argv, "--name-status", "-z", baseline, "--"],
            limit=max_files,
            env_overrides=env,
            output_limit_chars=_HARNESS_MAX_DIFF_CHARS,
        )
        tracked_files = list(tracked.get("files") or [])
        tracked_paths = {
            str(item.get("path") or "")
            for item in tracked_files
            if isinstance(item, dict)
        }
        changed_files = tracked_files + [
            {"path": path, "status": "??", "kind": "untracked"}
            for path in untracked_paths
            if path not in tracked_paths
        ]
        synthetic_untracked_paths = [
            str(item.get("path") or "")
            for item in changed_files
            if isinstance(item, dict)
            and str(item.get("kind") or "").strip().lower() == "untracked"
            and str(item.get("path") or "")
        ]
        diff_pathspecs = [
            ".",
            *[
                f":(exclude,literal){path}"
                for path in synthetic_untracked_paths
            ],
        ]
        stat = _run_process(
            [*diff_argv, "--stat", baseline, "--", *diff_pathspecs],
            cwd=repo,
            env_overrides=env,
            output_limit_chars=_HARNESS_MAX_DIFF_CHARS,
        )
        diff = _run_process(
            [*diff_argv, baseline, "--", *diff_pathspecs],
            cwd=repo,
            env_overrides=env,
            output_limit_chars=_HARNESS_MAX_DIFF_CHARS,
        )
        changes = {
            "ok": bool(
                tracked.get("ok")
                and untracked_result.get("ok")
            ),
            "counts": _counts_for_change_files(changed_files),
            "files": changed_files[:max_files],
            "truncated": bool(
                tracked.get("truncated")
                or untracked_result.get("stdout_truncated")
                or len(changed_files) > max_files
            ),
            "raw": untracked_result,
        }
        diff_result = {
            "ok": bool(
                stat.get("ok")
                and diff.get("ok")
                and tracked.get("ok")
                and untracked_result.get("ok")
            ),
            "scope": "harness_baseline",
            "base_branch": str(task.get("base_branch") or "main"),
            "branch_name": str(task.get("branch_name") or ""),
            "base_ref": baseline,
            "merge_base": baseline,
            "compare_ref": baseline,
            "stat": stat,
            "diff": diff,
            "changes": changes,
        }
        return {"diff": diff_result, "changes": changes}


def harness_git_diff(
    task_id: str,
    *,
    evidence_lease_id: Optional[str] = None,
) -> Dict[str, Any]:
    task = load_task(task_id)
    if str(task.get("kind") or "") != "harness_eval":
        raise HTTPException(status_code=403, detail="harness diff requires a harness task")
    evidence_workspace = begin_harness_evidence_read(
        task_id,
        evidence_lease_id=evidence_lease_id,
    )
    try:
        return _harness_neutral_git_snapshot(
            task,
            change_limit=_HARNESS_MAX_CHANGED_FILES + 1,
            repo_override=evidence_workspace,
        )["diff"]
    finally:
        end_harness_evidence_read(task_id)


def harness_git_changes(
    task_id: str,
    *,
    evidence_lease_id: Optional[str] = None,
) -> Dict[str, Any]:
    task = load_task(task_id)
    if str(task.get("kind") or "") != "harness_eval":
        raise HTTPException(status_code=403, detail="harness changes require a harness task")
    evidence_workspace = begin_harness_evidence_read(
        task_id,
        evidence_lease_id=evidence_lease_id,
    )
    try:
        return _harness_neutral_git_snapshot(
            task,
            change_limit=_HARNESS_MAX_CHANGED_FILES + 1,
            repo_override=evidence_workspace,
        )["changes"]
    finally:
        end_harness_evidence_read(task_id)


def commit_task(task_id: str, *, message: str) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _commit_task(task_id, message=message)


def _commit_task(task_id: str, *, message: str) -> Dict[str, Any]:
    msg = str(message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="commit message is required")
    if len(msg) > 2000:
        raise HTTPException(status_code=400, detail="commit message is too long")
    task = load_task(task_id)
    repo = _repo_path(task)
    status = _run_process(["git", "status", "--porcelain"], cwd=repo)
    if not status.get("ok"):
        _append_command(task, status, label="commit-status")
        save_task(task)
        return {"ok": False, "status": status, "error": "git status failed"}
    if not str(status.get("stdout") or "").strip():
        return {"ok": False, "status": status, "error": "no changes to commit"}
    add = _run_process(["git", "add", "-A"], cwd=repo)
    _append_command(task, add, label="git-add")
    if not add.get("ok"):
        save_task(task)
        return {"ok": False, "add": add, "error": "git add failed"}
    commit = _run_process(["git", "commit", "-m", msg], cwd=repo)
    _append_command(task, commit, label="git-commit")
    if commit.get("ok"):
        rev = _run_process(["git", "rev-parse", "HEAD"], cwd=repo)
        task["last_commit"] = str(rev.get("stdout") or "").strip()
    save_task(task)
    return {"ok": bool(commit.get("ok")), "status": status, "add": add, "commit": commit, "last_commit": task.get("last_commit")}


def checkpoint_task(
    task_id: str,
    *,
    message: str,
    run_id: Optional[str] = None,
    cycle: Optional[int] = None,
) -> Dict[str, Any]:
    msg = str(message or "").strip() or "Nexus coding agent checkpoint"
    if len(msg) > 2000:
        msg = msg[:2000]
    task = load_task(task_id)
    repo = _repo_path(task)
    command_results: List[Tuple[Dict[str, Any], str]] = []

    def persist_checkpoint(*, updates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        def apply(current: Dict[str, Any]) -> None:
            for result, label in command_results:
                _append_command(current, result, label=label)
            if updates:
                current.update(updates)

        return mutate_task(task_id, apply)

    status = _run_process(["git", "status", "--porcelain"], cwd=repo)
    if not status.get("ok"):
        command_results.append((status, "checkpoint-status"))
        persist_checkpoint()
        return {"ok": False, "changed": False, "status": status, "error": "git status failed"}
    if not str(status.get("stdout") or "").strip():
        persist_checkpoint()
        return {"ok": True, "changed": False, "status": status, "message": "no changes to checkpoint"}
    add = _run_process(["git", "add", "-A"], cwd=repo)
    command_results.append((add, "checkpoint-add"))
    if not add.get("ok"):
        persist_checkpoint()
        return {"ok": False, "changed": True, "add": add, "error": "git add failed"}
    commit = _run_process(["git", "commit", "-m", msg], cwd=repo)
    command_results.append((commit, "checkpoint-commit"))
    rev = {"ok": False, "stdout": ""}
    updates: Dict[str, Any] = {}
    if commit.get("ok"):
        rev = _run_process(["git", "rev-parse", "HEAD"], cwd=repo)
        commit_hash = str(rev.get("stdout") or "").strip()
        updates = {
            "last_commit": commit_hash,
            "last_checkpoint_commit": commit_hash,
            "last_checkpoint_at": _now(),
            "last_checkpoint_run_id": str(run_id or "").strip(),
            "last_checkpoint_cycle": int(cycle or 0),
        }
    persisted = persist_checkpoint(updates=updates)
    return {
        "ok": bool(commit.get("ok")),
        "changed": True,
        "status": status,
        "add": add,
        "commit": commit,
        "rev": rev,
        "last_commit": persisted.get("last_commit"),
    }


def push_task(
    task_id: str,
    *,
    remote: Optional[str] = None,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _push_task(
            task_id,
            remote=remote,
            git_token_value=git_token_value,
        )


def _push_task(
    task_id: str,
    *,
    remote: Optional[str] = None,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    remote_name = str(remote or "origin").strip() or "origin"
    if not re.match(r"^[A-Za-z0-9._-]+$", remote_name):
        raise HTTPException(status_code=400, detail="invalid remote")
    branch = str(task.get("branch_name") or "").strip()
    result = _run_process(
        ["git", "push", "-u", remote_name, branch],
        cwd=repo,
        timeout_sec=max(command_timeout_sec(), 300.0),
        use_git_credentials=True,
        git_token_value=git_token_value,
    )
    _append_command(task, result, label="git-push")
    if result.get("ok"):
        task["last_pushed_at"] = _now()
    save_task(task)
    return result


def _github_owner_repo(repo_url: str) -> Optional[Tuple[str, str]]:
    raw = str(repo_url or "").strip()
    path = ""
    if raw.startswith("git@github.com:"):
        path = raw.split(":", 1)[1]
    else:
        try:
            parts = urlsplit(raw)
        except Exception:
            return None
        if (parts.hostname or "").lower() != "github.com":
            return None
        path = parts.path.lstrip("/")
    bits = [part for part in path.split("/") if part]
    if len(bits) < 2:
        return None
    owner = bits[0]
    repo = bits[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return owner, repo


def _create_github_pr_api(
    *,
    repo_url: str,
    title: str,
    body: str,
    head: str,
    base: str,
    draft: bool,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    token = _effective_git_token(git_token_value)
    if not token:
        return {"ok": False, "error": "CODING_GIT_TOKEN or GITHUB_TOKEN is required for GitHub API PR creation"}
    owner_repo = _github_owner_repo(repo_url)
    if owner_repo is None:
        return {"ok": False, "error": "repo_url is not a GitHub repository URL"}
    owner, repo = owner_repo
    payload = json.dumps({"title": title, "body": body, "head": head, "base": base, "draft": bool(draft)}).encode("utf-8")
    req = urlrequest.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "nexus-coding-workspaces",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return {
                "ok": 200 <= int(resp.status) < 300,
                "status": int(resp.status),
                "url": data.get("html_url") if isinstance(data, dict) else None,
                "number": data.get("number") if isinstance(data, dict) else None,
                "response": data,
            }
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body_data: Any = json.loads(raw) if raw else {}
        except Exception:
            body_data = raw
        return {"ok": False, "status": int(exc.code), "error": "GitHub API PR creation failed", "response": body_data}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {_redact_text(str(exc))}"}


def _github_api_request(
    method: str,
    path: str,
    *,
    git_token_value: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    token = _effective_git_token(git_token_value)
    if not token:
        return {"ok": False, "error": "CODING_GIT_TOKEN or GITHUB_TOKEN is required for GitHub API access"}
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "nexus-coding-workspaces",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(
        f"https://api.github.com{path}",
        data=data,
        method=method.upper(),
        headers=headers,
    )
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else {}
            return {"ok": True, "status": int(resp.status), "body": body}
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body_data: Any = json.loads(raw) if raw else {}
        except Exception:
            body_data = raw
        return {"ok": False, "status": int(exc.code), "body": body_data}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {_redact_text(str(exc))}"}


def _ensure_github_repo_available(repo_url: str, *, git_token_value: Optional[str] = None) -> Dict[str, Any]:
    owner_repo = _github_owner_repo(repo_url)
    if owner_repo is None:
        return {"ok": False, "error": "repo_url is not a GitHub repository URL"}
    owner, repo = owner_repo
    existing = _github_api_request("GET", f"/repos/{owner}/{repo}", git_token_value=git_token_value)
    if existing.get("ok"):
        body = existing.get("body") if isinstance(existing.get("body"), dict) else {}
        return {"ok": True, "created": False, "empty": int(body.get("size") or 0) == 0, "body": body}
    if int(existing.get("status") or 0) != 404:
        return {"ok": False, "error": "GitHub repo lookup failed", "response": existing.get("body") or existing.get("error")}

    viewer = _github_api_request("GET", "/user", git_token_value=git_token_value)
    if not viewer.get("ok"):
        return {"ok": False, "error": "GitHub user lookup failed", "response": viewer.get("body") or viewer.get("error")}
    viewer_body = viewer.get("body") if isinstance(viewer.get("body"), dict) else {}
    login = str(viewer_body.get("login") or "").strip().lower()
    payload = {
        "name": repo,
        "description": f"Generated Nexus model integration workspace for {repo}",
        "private": True,
        "auto_init": False,
    }
    if owner.lower() == login:
        created = _github_api_request("POST", "/user/repos", git_token_value=git_token_value, payload=payload)
    else:
        created = _github_api_request("POST", f"/orgs/{owner}/repos", git_token_value=git_token_value, payload=payload)
    if not created.get("ok"):
        return {"ok": False, "error": "GitHub repo creation failed", "response": created.get("body") or created.get("error")}
    return {"ok": True, "created": True, "empty": True, "body": created.get("body") if isinstance(created.get("body"), dict) else {}}


def _ensure_model_integration_remote(
    task: Dict[str, Any],
    *,
    repo_url: str,
    git_token_value: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    remote_meta = _ensure_github_repo_available(repo_url, git_token_value=git_token_value)
    _append_command(
        task,
        {
            "ok": remote_meta.get("ok"),
            "returncode": 0 if remote_meta.get("ok") else 1,
            "argv": ["github-api", "repos.ensure"],
            "stdout": json.dumps(remote_meta.get("body") or {}, ensure_ascii=False),
            "stderr": "" if remote_meta.get("ok") else json.dumps(remote_meta.get("response") or remote_meta.get("error") or "", ensure_ascii=False),
            "duration_ms": 0,
        },
        label="github-repo-ensure",
    )
    if not remote_meta.get("ok"):
        task["status"] = "error"
        task["error"] = str(remote_meta.get("error") or "github repo ensure failed")
        return None
    return remote_meta


def _attach_model_integration_remote(
    task: Dict[str, Any],
    *,
    repo: Path,
    repo_url: str,
    base_branch: str,
    branch_name: str,
    git_token_value: Optional[str] = None,
    remote_meta: Optional[Dict[str, Any]] = None,
) -> bool:
    remote_meta = remote_meta if isinstance(remote_meta, dict) else _ensure_model_integration_remote(
        task,
        repo_url=repo_url,
        git_token_value=git_token_value,
    )
    if remote_meta is None:
        return False

    remote_result = _run_process(["git", "remote", "add", "origin", repo_url], cwd=repo, use_git_credentials=False)
    _append_command(task, remote_result, label="git-remote-add")
    if not remote_result.get("ok"):
        task["status"] = "error"
        task["error"] = "git remote add failed"
        return False

    push_base = _run_process(
        ["git", "push", "-u", "origin", base_branch],
        cwd=repo,
        timeout_sec=max(command_timeout_sec(), 300.0),
        use_git_credentials=True,
        git_token_value=git_token_value,
    )
    _append_command(task, push_base, label="git-push-base")
    if not push_base.get("ok"):
        task["status"] = "error"
        task["error"] = "initial base branch push failed"
        return False

    if branch_name != base_branch:
        push_branch = _run_process(
            ["git", "push", "-u", "origin", branch_name],
            cwd=repo,
            timeout_sec=max(command_timeout_sec(), 300.0),
            use_git_credentials=True,
            git_token_value=git_token_value,
        )
        _append_command(task, push_branch, label="git-push-branch")
        if not push_branch.get("ok"):
            task["status"] = "error"
            task["error"] = "initial working branch push failed"
            return False

    task["last_pushed_at"] = _now()
    return True


def create_pull_request(
    task_id: str,
    *,
    title: str,
    body: Optional[str],
    draft: bool = True,
    base_branch: Optional[str] = None,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        return _create_pull_request(
            task_id,
            title=title,
            body=body,
            draft=draft,
            base_branch=base_branch,
            git_token_value=git_token_value,
        )


def _create_pull_request(
    task_id: str,
    *,
    title: str,
    body: Optional[str],
    draft: bool = True,
    base_branch: Optional[str] = None,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    pr_title = str(title or "").strip()
    if not pr_title:
        raise HTTPException(status_code=400, detail="PR title is required")
    base = _base_branch(base_branch or str(task.get("base_branch") or "main"))
    branch = str(task.get("branch_name") or "").strip()
    if _effective_git_token(git_token_value):
        api_result = _create_github_pr_api(
            repo_url=str(task.get("repo_url") or ""),
            title=pr_title,
            body=str(body or "").strip(),
            head=branch,
            base=base,
            draft=draft,
            git_token_value=git_token_value,
        )
        _append_command(
            task,
            {
                "ok": api_result.get("ok"),
                "returncode": 0 if api_result.get("ok") else 1,
                "argv": ["github-api", "pulls.create"],
                "stdout": str(api_result.get("url") or ""),
                "stderr": "" if api_result.get("ok") else json.dumps(api_result.get("response") or api_result.get("error") or "", ensure_ascii=False),
                "duration_ms": 0,
            },
            label="github-pr-create",
        )
        if api_result.get("ok"):
            task["last_pr_at"] = _now()
            task["last_pr_output"] = str(api_result.get("url") or "")
        save_task(task)
        return api_result
    if shutil.which("gh") is None:
        return {
            "ok": False,
            "error": "GitHub CLI is not installed and no GitHub token is configured",
            "suggested_command": (
                f"gh pr create --base {base} --head {branch} "
                f"--title {json.dumps(pr_title)} --body <body>"
            ),
        }
    argv = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", pr_title, "--body", str(body or "").strip()]
    if draft:
        argv.append("--draft")
    result = _run_process(
        argv,
        cwd=repo,
        timeout_sec=max(command_timeout_sec(), 300.0),
        use_git_credentials=True,
        git_token_value=git_token_value,
    )
    _append_command(task, result, label="gh-pr-create")
    if result.get("ok"):
        task["last_pr_at"] = _now()
        task["last_pr_output"] = str(result.get("stdout") or "").strip()
    save_task(task)
    return result


def coding_state_snapshot(task_id: str) -> Dict[str, Any]:
    """Return durable controller-owned state for model hydration and the UI."""
    task = load_task(task_id)
    events = [item for item in (task.get("agent_events") or []) if isinstance(item, dict)]
    commands = [item for item in (task.get("commands") or []) if isinstance(item, dict)]
    last_edit_at = 0.0
    last_edited_files: List[str] = []
    last_validation_at = 0.0
    last_validation_command: List[str] = []
    last_validation_ok: Optional[bool] = None
    last_diff_review_at = 0.0
    last_action = ""
    blockers: List[str] = []
    for event in events:
        event_type = str(event.get("type") or "")
        ts = float(event.get("ts") or 0)
        if event_type == "tool_finished":
            name = str(event.get("name") or "")
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            last_action = name
            if name in {"coding_write_file", "coding_replace_text", "coding_apply_patch"} and bool(result.get("ok", True)):
                last_edit_at = max(last_edit_at, ts)
            if name == "coding_git_diff" and bool(result.get("ok", True)):
                last_diff_review_at = max(last_diff_review_at, ts)
        if event_type in {"finish_gate", "no_progress_limit", "failed"}:
            text = str(event.get("summary") or event.get("error") or "").strip()
            if text:
                blockers.append(text[:500])
    for command in commands:
        label = str(command.get("label") or "")
        if label in {"agent-command", "command"}:
            last_validation_at = float(command.get("ts") or last_validation_at)
            last_validation_command = [str(item) for item in (command.get("argv") or [])]
            last_validation_ok = bool(command.get("ok"))
    change_summary = git_change_summary(task_id)
    files = change_summary.get("files") if isinstance(change_summary.get("files"), list) else []
    last_edited_files = [str(item.get("path") or "") for item in files if isinstance(item, dict) and item.get("path")]
    head = git_head(task_id)
    mission = normalize_coding_mission(task)
    cycle = int(task.get("agent_cycle") or 0)
    phase = "editing"
    if last_validation_at >= last_edit_at and last_validation_at:
        phase = "reviewing" if last_diff_review_at < last_edit_at else "finalizing"
    return {
        "schema": "nexus_coding_state.v1",
        "generated_at": _now(),
        "mission": mission,
        "branch": {
            "base_branch": task.get("base_branch") or "main",
            "branch_name": task.get("branch_name") or "",
            "start_head": task.get("agent_start_head") or "",
            "current_head": head.get("commit") or "",
            "last_checkpoint_commit": task.get("last_checkpoint_commit") or "",
        },
        "progress": {
            "cycle": cycle,
            "current_phase": phase,
            "last_meaningful_action": last_action,
            "next_recommended_action": (
                "continue the current project-plan milestone"
                if not files
                else "validate changes"
                if last_validation_at < last_edit_at
                else "review diff"
                if last_diff_review_at < last_edit_at
                else "finish the mission"
            ),
        },
        "plan": normalize_project_plan(task.get("project_plan"), fallback_goal=mission["goal"]),
        "changes": {
            "changed_files": files,
            "counts": change_summary.get("counts") or {},
            "last_edit_at": last_edit_at,
            "last_edited_files": last_edited_files,
        },
        "validation": {
            "last_validation_command": last_validation_command,
            "last_validation_ok": last_validation_ok,
            "last_validation_at": last_validation_at,
            "validation_after_latest_edit": bool(last_validation_at and last_validation_at >= last_edit_at),
        },
        "diff_review": {
            "last_diff_review_at": last_diff_review_at,
            "diff_reviewed_after_latest_edit": bool(last_diff_review_at and last_diff_review_at >= last_edit_at),
        },
        "blockers": blockers[-8:],
        "recent_guidance": (task.get("guidance_messages") or [])[-8:],
        "recent_events": events[-20:],
    }


def list_tree(task_id: str, *, path: Optional[str] = None, limit: int = 250) -> Dict[str, Any]:
    task = load_task(task_id)
    target = _resolve_repo_child(task, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="path is not a directory")
    max_items = max(1, min(int(limit or 250), 1000))
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in _TREE_SKIP:
            continue
        rel = child.relative_to(_repo_path(task)).as_posix()
        item = {"name": child.name, "path": rel, "type": "dir" if child.is_dir() else "file"}
        if child.is_file():
            try:
                item["size"] = child.stat().st_size
            except Exception:
                pass
        entries.append(item)
        if len(entries) >= max_items:
            break
    return {"path": str(path or ""), "entries": entries, "truncated": len(entries) >= max_items}


def read_file(task_id: str, *, path: str) -> Dict[str, Any]:
    task = load_task(task_id)
    target = _resolve_repo_child(task, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    size = target.stat().st_size
    if size > file_max_bytes():
        raise HTTPException(status_code=413, detail="file is too large for the coding file API")
    raw = target.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return {"path": str(path), "size": size, "content": text}


def _resolve_harness_evidence_path(
    task: Dict[str, Any],
    path: str,
    *,
    repo_override: Optional[Path] = None,
) -> Tuple[Path, Optional[int]]:
    rel = str(path)
    candidate = Path(rel)
    if (
        not rel
        or "\x00" in rel
        or candidate.is_absolute()
        or any(part in {"", ".", "..", ".git"} for part in candidate.parts)
    ):
        raise HTTPException(status_code=400, detail="invalid harness evidence path")
    repo = repo_override.resolve() if repo_override is not None else _repo_path(task)
    if not repo.is_dir() or repo.is_symlink():
        raise HTTPException(status_code=409, detail="harness evidence workspace is unavailable")
    target = repo.joinpath(candidate)
    current = repo
    for part in candidate.parts:
        current = current.joinpath(part)
        try:
            if current.is_symlink():
                return target, current.lstat().st_size
        except OSError:
            break
    return target, None


def _read_harness_file_evidence(
    task: Dict[str, Any],
    *,
    path: str,
    repo_override: Optional[Path] = None,
) -> Dict[str, Any]:
    target, symlink_size = _resolve_harness_evidence_path(
        task,
        path,
        repo_override=repo_override,
    )
    if symlink_size is not None:
        return {
            "path": str(path),
            "size": symlink_size,
            "encoding": "symlink",
            "content": None,
        }
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    file_stat = target.stat()
    size = file_stat.st_size
    if size > _HARNESS_MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file is too large for the coding file API")
    raw = target.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = None
    if content is None or "\x00" in content:
        return {"path": str(path), "size": size, "encoding": "binary", "content": None}
    mode = "100755" if file_stat.st_mode & 0o111 else "100644"
    return {
        "path": str(path),
        "size": size,
        "mode": mode,
        "encoding": "utf-8",
        "content": content,
    }


def read_harness_file_evidence(
    task_id: str,
    *,
    path: str,
    evidence_lease_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read exact text evidence without lossy decoding for disposable harness tasks."""
    task = load_task(task_id)
    if str(task.get("kind") or "") != "harness_eval":
        raise HTTPException(status_code=403, detail="only coding harness tasks may be read here")
    evidence_workspace = begin_harness_evidence_read(
        task_id,
        evidence_lease_id=evidence_lease_id,
    )
    try:
        return _read_harness_file_evidence(
            task,
            path=path,
            repo_override=evidence_workspace,
        )
    finally:
        end_harness_evidence_read(task_id)


def read_file_lines(task_id: str, *, path: str, start_line: int = 1, line_count: int = 200) -> Dict[str, Any]:
    task = load_task(task_id)
    target = _resolve_repo_child(task, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    size = target.stat().st_size
    if size > file_max_bytes():
        raise HTTPException(status_code=413, detail="file is too large for the coding file API")
    start = max(1, int(start_line or 1))
    count = max(1, min(int(line_count or 200), 2000))
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    selected = lines[start - 1 : start - 1 + count]
    end_line = start + len(selected) - 1 if selected else start - 1
    return {
        "path": str(path),
        "size": size,
        "start_line": start,
        "end_line": end_line,
        "line_count": len(selected),
        "total_lines": total,
        "content": "".join(selected),
        "truncated": end_line < total,
    }


def replace_text(
    task_id: str,
    *,
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: Optional[int] = 1,
) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        task = load_task(task_id)
        rel = str(path or "").strip()
        if not rel:
            raise HTTPException(status_code=400, detail="path is required")
        old = str(old_text or "")
        if not old:
            raise HTTPException(status_code=400, detail="old_text is required")
        target = _resolve_repo_child(task, rel)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        size = target.stat().st_size
        if size > file_max_bytes():
            raise HTTPException(status_code=413, detail="file is too large for the coding file API")
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count <= 0:
            return {"ok": False, "path": rel, "replacements": 0, "error": "old_text was not found"}
        expected = None if expected_replacements is None else int(expected_replacements)
        if expected is not None and expected >= 0 and count != expected:
            return {"ok": False, "path": rel, "replacements": count, "expected_replacements": expected, "error": "replacement count did not match expected_replacements"}
        updated = text.replace(old, str(new_text or ""))
        data = updated.encode("utf-8")
        if len(data) > file_max_bytes():
            raise HTTPException(status_code=413, detail="replacement result is too large")
        target.write_bytes(data)
        task["last_file_write_at"] = _now()
        save_task(task)
        return {"ok": True, "path": rel, "replacements": count, "bytes": len(data)}


def write_file(task_id: str, *, path: str, content: str) -> Dict[str, Any]:
    with _harness_mutation_guard(task_id):
        task = load_task(task_id)
        rel = str(path or "").strip()
        if not rel:
            raise HTTPException(status_code=400, detail="path is required")
        target = _resolve_repo_child(task, rel)
        data = str(content or "").encode("utf-8")
        if len(data) > file_max_bytes():
            raise HTTPException(status_code=413, detail="content is too large for the coding file API")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        task["last_file_write_at"] = _now()
        save_task(task)
        return {"ok": True, "path": rel, "bytes": len(data)}


def delete_task(
    task_id: str,
    *,
    _allow_harness: bool = False,
    _allow_initializing: bool = False,
) -> Dict[str, Any]:
    task = load_task(task_id)
    if (
        str(task.get("status") or "").strip().lower() == "initializing"
        and not _allow_initializing
    ):
        raise HTTPException(status_code=409, detail="coding task is still initializing")
    if str(task.get("kind") or "") == "harness_eval" and not _allow_harness:
        raise HTTPException(
            status_code=403,
            detail="harness tasks require the guarded harness deletion endpoint",
        )
    meta = _task_path(task_id)
    with _json_lock(meta):
        path = _task_workspace(task_id)
        root = workspace_root()
        _ensure_inside(root, path)
        if path.exists():
            shutil.rmtree(path)
        _mark_task_deleted(meta)
        try:
            meta.unlink()
        except FileNotFoundError:
            pass
    return {"ok": True, "task_id": task_id, "deleted_workspace": str(path), "repo_url": redact_repo_url(str(task.get("repo_url") or ""))}


def delete_harness_task(
    task_id: str,
    *,
    evidence_lease_id: Optional[str] = None,
    _allow_initializing: bool = False,
) -> Dict[str, Any]:
    """Atomically refuse deletion while a harness validation command is active."""
    with _HARNESS_VALIDATIONS_GUARD:
        active_lease = _active_harness_evidence_lease_locked(task_id)
        provided_lease = str(evidence_lease_id or "")
        if active_lease != provided_lease and (active_lease or provided_lease):
            raise HTTPException(status_code=409, detail="coding harness evidence lease is not active")
        if task_id in _ACTIVE_HARNESS_RUN_STARTS:
            raise HTTPException(status_code=409, detail="coding harness agent run is starting")
        if task_id in _ACTIVE_HARNESS_VALIDATIONS:
            raise HTTPException(status_code=409, detail="coding harness validation is still active")
        if _ACTIVE_HARNESS_AGENT_TOOLS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness agent tool is still active")
        if _ACTIVE_HARNESS_EVIDENCE_READS.get(task_id, 0) > 0:
            raise HTTPException(status_code=409, detail="coding harness evidence read is still active")
        task = load_task(task_id)
        if str(task.get("kind") or "") != "harness_eval":
            raise HTTPException(status_code=403, detail="only coding harness tasks may be deleted here")
        agent_status = str(task.get("agent_status") or "idle").strip().lower()
        if agent_status in {"queued", "running", "stopping", "pausing"}:
            raise HTTPException(status_code=409, detail="coding harness task is still active")
        result = delete_task(
            task_id,
            _allow_harness=True,
            _allow_initializing=_allow_initializing,
        )
        if active_lease:
            removed = _ACTIVE_HARNESS_EVIDENCE_LEASES.pop(active_lease, None)
            if isinstance(removed, dict):
                _cleanup_harness_evidence_lease(removed)
        return result


def cleanup_expired_harness_tasks(*, now: Optional[float] = None) -> Dict[str, Any]:
    """Remove expired terminal eval workspaces left by a disconnected client."""
    _ensure_enabled()
    current = float(now if now is not None else _now())
    terminal_statuses = {
        "completed",
        "failed",
        "failed_finalization",
        "failed_publish",
        "paused",
        "stopped",
        "interrupted",
        "idle_waiting",
    }
    purged: List[str] = []
    failures: Dict[str, str] = {}
    for path in tasks_dir().glob("code_*.json"):
        try:
            task = _read_json(path)
            if str(task.get("kind") or "") != "harness_eval":
                continue
            expires_at = float(task.get("harness_expires_at") or 0)
            agent_status = str(task.get("agent_status") or "idle").strip().lower()
            task_status = str(task.get("status") or "").strip().lower()
            finished_at = float(task.get("agent_finished_at") or task.get("updated_at") or 0)
            abandoned_idle = (
                task_status in {"initializing", "ready"} and agent_status == "idle"
            )
            if (
                expires_at <= 0
                or expires_at > current
                or (
                    task_status != "error"
                    and agent_status not in terminal_statuses
                    and not abandoned_idle
                )
                or finished_at <= 0
                or current - finished_at < 30.0
            ):
                continue
            task_id = str(task.get("id") or path.stem)
            delete_harness_task(task_id, _allow_initializing=abandoned_idle)
            purged.append(task_id)
        except Exception as exc:
            failures[path.stem] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
    return {"ok": not failures, "purged": purged, "failures": failures}


def archive_task(task_id: str, *, actor: Optional[str] = None, reason: Optional[str] = None) -> Dict[str, Any]:
    task = load_task(task_id)
    if str(task.get("kind") or "") == "harness_eval":
        raise HTTPException(
            status_code=403,
            detail="disposable harness tasks cannot be archived",
        )
    task_path = _task_path(task_id)
    workspace_path = _task_workspace(task_id)
    archive_id = f"{task_id}.{int(_now())}.{secrets.token_hex(4)}"

    _ensure_inside(tasks_dir(), task_path)
    _ensure_inside(workspace_root(), workspace_path)

    task_archive_root = _archived_tasks_dir()
    workspace_archive_root = _archived_workspaces_dir()
    task_archive_root.mkdir(parents=True, exist_ok=True)
    workspace_archive_root.mkdir(parents=True, exist_ok=True)

    archived_task_path = task_archive_root.joinpath(f"{archive_id}.json")
    archived_workspace_path = workspace_archive_root.joinpath(archive_id)
    manifest_path = task_archive_root.joinpath(f"{archive_id}.manifest.json")

    if task_path.exists():
        shutil.move(str(task_path), str(archived_task_path))
    else:
        archived_task_path.write_text(json.dumps(task, indent=2, sort_keys=True), encoding="utf-8")

    if workspace_path.exists():
        shutil.move(str(workspace_path), str(archived_workspace_path))

    archived_at = _now()
    manifest = _normalize_archive_manifest(
        {
            "archive_id": archive_id,
            "task_id": task_id,
            "archived_at": archived_at,
            "actor": str(actor or "").strip(),
            "reason": str(reason or "manual_archive").strip() or "manual_archive",
            "repo_url": redact_repo_url(str(task.get("repo_url") or "")),
            "status": str(task.get("status") or ""),
            "agent_status": str(task.get("agent_status") or ""),
            "metadata_error": task.get("metadata_error") if isinstance(task.get("metadata_error"), dict) else None,
            "task_path": str(archived_task_path),
            "workspace_path": str(archived_workspace_path) if archived_workspace_path.exists() else "",
        },
        manifest_path=manifest_path,
        task=task,
    )
    _write_json(manifest_path, manifest)

    return {
        "ok": True,
        "task_id": task_id,
        "archive_id": archive_id,
        "archived_task": str(archived_task_path),
        "archived_workspace": str(archived_workspace_path) if archived_workspace_path.exists() else "",
        "manifest": str(manifest_path),
        "repo_url": redact_repo_url(str(task.get("repo_url") or "")),
    }


def list_archived_tasks(limit: int = 100) -> List[Dict[str, Any]]:
    _ensure_enabled()
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for root in _archive_task_roots():
        if not root.exists():
            continue
        for manifest_path in root.glob("code_*.manifest.json"):
            archive_id = manifest_path.name[: -len(".manifest.json")]
            if archive_id in seen:
                continue
            seen.add(archive_id)
            try:
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                raw_manifest = {}
            task_path = manifest_path.with_name(f"{archive_id}.json")
            task = _load_archived_task_json(task_path)
            manifest = _normalize_archive_manifest(raw_manifest if isinstance(raw_manifest, dict) else {}, manifest_path=manifest_path, task=task)
            if manifest != raw_manifest:
                _write_json(manifest_path, manifest)
            findings = _apply_finding_reviews(_read_findings_log(Path(str(manifest.get("findings_path") or "")), limit=200))
            analysis = manifest.get("analysis") if isinstance(manifest.get("analysis"), dict) else {}
            retention = manifest.get("retention") if isinstance(manifest.get("retention"), dict) else {}
            items.append(
                {
                    "archive_id": archive_id,
                    "task_id": str(manifest.get("task_id") or task.get("id") or ""),
                    "archived_at": manifest.get("archived_at"),
                    "actor": manifest.get("actor") or "",
                    "reason": manifest.get("reason") or "",
                    "owner": str(task.get("owner") or ""),
                    "owner_user_id": task.get("owner_user_id"),
                    "prompt": str(task.get("prompt") or "")[:1200],
                    "coding_model": str(task.get("coding_model") or ""),
                    "status": str(manifest.get("status") or task.get("status") or ""),
                    "agent_status": str(manifest.get("agent_status") or task.get("agent_status") or ""),
                    "repo_url": str(manifest.get("repo_url") or task.get("repo_url") or ""),
                    "base_branch": str(task.get("base_branch") or ""),
                    "branch_name": str(task.get("branch_name") or ""),
                    "paths": {
                        "manifest": str(manifest_path),
                        "task": str(manifest.get("task_path") or task_path),
                        "workspace": str(manifest.get("workspace_path") or ""),
                        "findings": str(manifest.get("findings_path") or ""),
                        "external_brief": str(manifest.get("external_brief_path") or ""),
                    },
                    "analysis": analysis,
                    "retention": retention,
                    "findings": findings,
                }
            )
    items.sort(key=lambda item: float(item.get("archived_at") or 0), reverse=True)
    return items[: max(1, min(int(limit or 100), 500))]


def get_archived_task(archive_id: str) -> Dict[str, Any]:
    archive_id = _validate_archive_id(archive_id)
    for item in list_archived_tasks(limit=500):
        if str(item.get("archive_id") or "") == archive_id:
            return item
    raise HTTPException(status_code=404, detail="archived coding task not found")


def update_archived_task_settings(
    archive_id: str,
    *,
    preserve: Optional[bool] = None,
    delete_after_ts: Optional[float] = None,
    analysis_mode: Optional[str] = None,
    analysis_target: Optional[str] = None,
    analysis_model: Optional[str] = None,
) -> Dict[str, Any]:
    manifest_path = _archive_manifest_path(archive_id)
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    task = _load_archived_task_json(_archive_task_json_path(archive_id))
    manifest = _normalize_archive_manifest(raw_manifest if isinstance(raw_manifest, dict) else {}, manifest_path=manifest_path, task=task)
    retention = dict(manifest.get("retention") if isinstance(manifest.get("retention"), dict) else {})
    analysis = dict(manifest.get("analysis") if isinstance(manifest.get("analysis"), dict) else {})

    if preserve is not None:
        retention["preserve"] = bool(preserve)
        if retention["preserve"]:
            retention["delete_after_ts"] = 0
        elif int(retention.get("delete_after_ts") or 0) <= 0:
            retention["delete_after_ts"] = _default_archive_delete_after(float(manifest.get("archived_at") or _now()))

    if delete_after_ts is not None and not bool(retention.get("preserve")):
        try:
            retention["delete_after_ts"] = max(0, int(float(delete_after_ts)))
        except Exception:
            raise HTTPException(status_code=400, detail="delete_after_ts must be a number")

    if analysis_mode is not None:
        mode = str(analysis_mode or "").strip().lower()
        if mode not in _ARCHIVE_ANALYSIS_MODES:
            raise HTTPException(status_code=400, detail="analysis_mode must be one of manual, idle, immediate")
        analysis["requested_mode"] = mode
        analysis["last_requested_at"] = _now()
        if mode == "manual" and str(analysis.get("target") or "local") != "local":
            analysis["status"] = "manual"
        elif str(analysis.get("target") or "local") == "local":
            analysis["status"] = "pending"

    if analysis_target is not None:
        target = str(analysis_target or "").strip().lower()
        if target not in _ARCHIVE_ANALYSIS_TARGETS:
            raise HTTPException(status_code=400, detail="analysis_target must be one of local, external, human, none")
        analysis["target"] = target
        analysis["last_requested_at"] = _now()
        if target == "local":
            analysis["status"] = "pending"
        elif target == "external":
            analysis["status"] = "external_pending"
            analysis["local_model"] = ""
        elif target == "human":
            analysis["status"] = "manual"
            analysis["local_model"] = ""
        else:
            analysis["status"] = "disabled"
            analysis["local_model"] = ""

    if analysis_model is not None:
        model = str(analysis_model or "").strip()
        analysis["local_model"] = model
        if str(analysis.get("target") or "local") == "local" and analysis.get("local_model"):
            analysis["status"] = "pending"
            analysis["last_requested_at"] = _now()

    manifest["retention"] = _archive_default_retention(archived_at=float(manifest.get("archived_at") or _now()), retention=retention)
    manifest["analysis"] = _archive_default_analysis(task, archived_at=float(manifest.get("archived_at") or _now()), analysis=analysis)
    _write_json(manifest_path, manifest)
    if str(manifest["analysis"].get("target") or "").strip().lower() == "external":
        _write_archive_external_brief(archive_id, manifest, task)
    return get_archived_task(archive_id)


def mark_archived_analysis(
    archive_id: str,
    *,
    status: str,
    summary: str = "",
    error: str = "",
    started_at: Optional[float] = None,
    finished_at: Optional[float] = None,
    findings_count: Optional[int] = None,
) -> Dict[str, Any]:
    manifest_path = _archive_manifest_path(archive_id)
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    task = _load_archived_task_json(_archive_task_json_path(archive_id))
    manifest = _normalize_archive_manifest(raw_manifest if isinstance(raw_manifest, dict) else {}, manifest_path=manifest_path, task=task)
    analysis = dict(manifest.get("analysis") if isinstance(manifest.get("analysis"), dict) else {})
    analysis["status"] = str(status or analysis.get("status") or "pending")
    if started_at is not None:
        analysis["last_started_at"] = float(started_at)
    if finished_at is not None:
        analysis["last_finished_at"] = float(finished_at)
    if summary:
        analysis["last_summary"] = str(summary)[:2000]
    if error:
        analysis["last_error"] = str(error)[:4000]
    if findings_count is not None:
        analysis["findings_count"] = int(findings_count)
    manifest["analysis"] = _archive_default_analysis(task, archived_at=float(manifest.get("archived_at") or _now()), analysis=analysis)
    _write_json(manifest_path, manifest)
    return get_archived_task(archive_id)


def append_archived_finding(archive_id: str, *, entry: Dict[str, Any]) -> Dict[str, Any]:
    archive = get_archived_task(archive_id)
    findings_path = Path(str(((archive.get("paths") or {}).get("findings") or ""))).resolve()
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(entry)
    payload.setdefault("ts", _now())
    with findings_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    findings = _read_findings_log(findings_path, limit=500)
    summary = str(payload.get("summary") or payload.get("text") or "")[:2000]
    return mark_archived_analysis(
        archive_id,
        status=str(payload.get("status") or "completed"),
        summary=summary,
        error=str(payload.get("error") or ""),
        finished_at=float(payload.get("ts") or _now()),
        findings_count=len(findings),
    )


def review_archived_finding(
    archive_id: str,
    *,
    finding_ts: float,
    verdict: str,
    note: str = "",
    actor: str = "nexus-admin",
) -> Dict[str, Any]:
    review_verdict = str(verdict or "").strip().lower()
    if review_verdict not in _FINDING_REVIEW_VERDICTS:
        raise HTTPException(status_code=400, detail="verdict must be invalid or superseded")
    archive = get_archived_task(archive_id)
    findings_path = Path(str(((archive.get("paths") or {}).get("findings") or ""))).resolve()
    raw_items = _read_findings_log(findings_path, limit=1000)
    try:
        target_ts = int(float(finding_ts))
    except Exception:
        raise HTTPException(status_code=400, detail="finding_ts must be a number")
    target_exists = any(
        isinstance(item, dict)
        and str(item.get("kind") or "") != "sentinel_archive_review"
        and int(float(item.get("ts") or 0)) == target_ts
        for item in raw_items
        if isinstance(item, dict)
    )
    if not target_exists:
        raise HTTPException(status_code=404, detail="finding not found in archive log")
    payload = {
        "ts": _now(),
        "kind": "sentinel_archive_review",
        "actor": str(actor or "nexus-admin").strip() or "nexus-admin",
        "status": "reviewed",
        "reviewed_finding_ts": target_ts,
        "review_verdict": review_verdict,
        "note": str(note or "").strip()[:2000],
        "summary": f"Finding marked {review_verdict}.",
    }
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    with findings_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return get_archived_task(archive_id)


def purge_archived_task(archive_id: str) -> Dict[str, Any]:
    archive = get_archived_task(archive_id)
    paths = archive.get("paths") if isinstance(archive.get("paths"), dict) else {}
    removed: List[str] = []
    for key in ("task", "manifest", "findings", "external_brief"):
        path = Path(str(paths.get(key) or "")).resolve()
        if not path.exists():
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
    workspace = Path(str(paths.get("workspace") or "")).resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
        removed.append(str(workspace))
    return {"ok": True, "archive_id": archive_id, "removed": removed}


def cleanup_archived_tasks(*, now: Optional[float] = None) -> Dict[str, Any]:
    current = float(now if now is not None else _now())
    purged: List[Dict[str, Any]] = []
    for item in list_archived_tasks(limit=500):
        retention = item.get("retention") if isinstance(item.get("retention"), dict) else {}
        if bool(retention.get("preserve")):
            continue
        delete_after_ts = int(retention.get("delete_after_ts") or 0)
        if delete_after_ts <= 0 or delete_after_ts > int(current):
            continue
        result = purge_archived_task(str(item.get("archive_id") or ""))
        purged.append(
            {
                "archive_id": item.get("archive_id"),
                "task_id": item.get("task_id"),
                "workspace_path": ((item.get("paths") or {}).get("workspace") if isinstance(item.get("paths"), dict) else ""),
                "removed": result.get("removed") if isinstance(result, dict) else [],
            }
        )
    return {"ok": True, "purged": purged, "count": len(purged)}


def inspect_archived_task(archive_id: str, *, max_diff_chars: int = 12000) -> Dict[str, Any]:
    archive = get_archived_task(archive_id)
    task = _load_archived_task_json(Path(str(((archive.get("paths") or {}).get("task") or ""))))
    manifest_path = Path(str(((archive.get("paths") or {}).get("manifest") or "")))
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest = _normalize_archive_manifest(raw_manifest if isinstance(raw_manifest, dict) else {}, manifest_path=manifest_path, task=task)
    diff_snapshot = _archive_diff_snapshot(task, manifest, max_diff_chars=max_diff_chars)
    stop_diagnostics = archive_stop_diagnostics(task, manifest, redact=_redact_text)
    return {
        "ok": True,
        "archive": archive,
        "task": {
            "id": task.get("id") or archive.get("task_id"),
            "prompt": str(task.get("prompt") or ""),
            "status": str(task.get("status") or manifest.get("status") or ""),
            "agent_status": str(task.get("agent_status") or manifest.get("agent_status") or ""),
            "coding_model": str(task.get("coding_model") or ""),
            "owner": str(task.get("owner") or ""),
            "base_branch": str(task.get("base_branch") or "main"),
            "branch_name": str(task.get("branch_name") or ""),
            "commands": task.get("commands")[-12:] if isinstance(task.get("commands"), list) else [],
            "agent_events": task.get("agent_events")[-20:] if isinstance(task.get("agent_events"), list) else [],
        },
        "terminal_run": redacted_archive_run(task, manifest, redact=_redact_text),
        "stop_diagnostics": stop_diagnostics,
        "diff": diff_snapshot,
        "heuristics": _archive_heuristic_findings(task, manifest, diff_snapshot),
    }


def agent_brief(task_id: str, *, coding_model: Optional[str] = None) -> Dict[str, Any]:
    task = load_task(task_id)
    task_public = public_task(task)
    prompt = str(task.get("prompt") or "").strip() or "(no task prompt recorded)"
    branch = str(task.get("branch_name") or "").strip()
    base = str(task.get("base_branch") or "").strip()
    model = str(coding_model or task.get("coding_model") or "").strip()
    integration = task.get("integration") if isinstance(task.get("integration"), dict) else None
    seed_files = task.get("seed_files") if isinstance(task.get("seed_files"), list) else []
    seed_file_lines = ""
    if seed_files:
        seed_file_lines = "\nGenerated scaffold files:\n" + "\n".join(f"- {item}" for item in seed_files[:40]) + "\n"
    if str(task.get("kind") or "") == "model_integration" and integration is not None:
        strategy = str(integration.get("integration_strategy") or "").strip()
        if strategy == "existing_vllm_model":
            text = f"""Nexus coding task: {task.get("id")}

Goal:
{prompt}

Model integration:
- HuggingFace model: {integration.get("model_id")}
- Source URL: {integration.get("source_url")}
- Runtime: {integration.get("runtime")}
- Route kind: {integration.get("route_kind")}
- Integration strategy: existing vLLM model lane
- Existing backend lane: {integration.get("backend_class")}
- Target host: {(integration.get("deployment_target") or {}).get("host")}
- Preferred coding model: {model or "default"}

Workspace:
- Base branch: {base}
- Working branch: {branch}
{seed_file_lines}
Use the Nexus Coding API for workspace operations. Prefer a tight loop:
1. Review the generated task files and integration_request.json, then inspect existing vLLM lane files such as docker-compose.vllm-*.yml, deploy/topology/production.json, model_aliases.json, and relevant docs.
2. Add the model as an available model for the existing backend lane. Do not create a new backend class, service directory, registrar, or lifecycle backend unless the existing vLLM lane is demonstrably unsuitable.
3. Preserve existing repository files, especially README.md. Patch existing docs narrowly or use generated integration notes.
4. Run targeted checks with POST /v1/coding/tasks/{task.get("id")}/command.
5. Review GET /v1/coding/tasks/{task.get("id")}/diff before finishing.

Constraints:
- Work only inside this task workspace repo.
- Treat plain vLLM chat/embedding models as model availability/configuration changes on stackrot or ada2 lanes, not new backends.
- Commands are argv arrays, not shell strings.
- Blocked git operations include reset, clean, rebase, merge, restore, rm, and filter-branch.
"""
            return {"task": task_public, "brief": text}

        text = f"""Nexus coding task: {task.get("id")}

Goal:
{prompt}

Model integration:
- HuggingFace model: {integration.get("model_id")}
- Source URL: {integration.get("source_url")}
- Runtime: {integration.get("runtime")}
- Route kind: {integration.get("route_kind")}
- Containerize: {bool(integration.get("containerize"))}
- Shim required: {bool(integration.get("shim_required"))}
- Service name: {integration.get("service_name")}
- Backend class: {integration.get("backend_class")}
- Preferred coding model: {model or "default"}

Workspace:
- Base branch: {base}
- Working branch: {branch}
{seed_file_lines}

Use the Nexus Coding API for workspace operations. Prefer a tight loop:
1. Review README.md, AGENT_TASK.md, integration_request.json, and generated integration notes. If a root README already existed, the scaffold keeps generated notes under integration/.
2. Fill in the generated scaffold under services/ or host_native/.
3. Update integration/backend-config-snippet.yaml and integration/lifecycle.backend.json.
4. Run targeted checks with POST /v1/coding/tasks/{task.get("id")}/command.
5. Review GET /v1/coding/tasks/{task.get("id")}/diff.

Constraints:
- Work only inside this task workspace repo.
- Keep the resulting backend compatible with the expected OpenAI-style route.
- Do not replace an existing root README or broad documentation file wholesale; make focused patches.
- Commands are argv arrays, not shell strings.
- Blocked git operations include reset, clean, rebase, merge, restore, rm, and filter-branch.
"""
        return {"task": task_public, "brief": text}

    text = f"""Nexus coding task: {task.get("id")}

Goal:
{prompt}

Repository:
- URL: {redact_repo_url(str(task.get("repo_url") or ""))}
- Base branch: {base}
- Working branch: {branch}
- Preferred coding model: {model or "default"}

Use the Nexus Coding API for workspace operations. Prefer a tight loop:
1. Inspect files with GET /v1/coding/tasks/{task.get("id")}/tree and /file.
2. Make focused edits with PUT /v1/coding/tasks/{task.get("id")}/file or bounded commands.
3. Run targeted checks with POST /v1/coding/tasks/{task.get("id")}/command.
4. Review GET /v1/coding/tasks/{task.get("id")}/diff.
5. Commit, push, and open a draft PR only after tests and diff review.

Constraints:
- Work only inside this task workspace clone.
- Do not edit unrelated files, replace broad documentation wholesale, or force-push.
- Commands are argv arrays, not shell strings.
- Blocked git operations include reset, clean, rebase, merge, restore, rm, and filter-branch.
"""
    return {"task": task_public, "brief": text}


def public_task(task: Dict[str, Any], *, include_commands: bool = True) -> Dict[str, Any]:
    agent_events = task.get("agent_events")
    if not isinstance(agent_events, list):
        agent_events = []
    guidance_messages = task.get("guidance_messages")
    if not isinstance(guidance_messages, list):
        guidance_messages = []
    run_history = task.get("agent_runs")
    if not isinstance(run_history, list):
        run_history = []
    now = _now()
    created_at = float(task.get("created_at") or now)
    workspace_elapsed = int(max(0.0, now - created_at)) if created_at > 0 else 0
    agent_started = float(task.get("agent_started_at") or 0)
    agent_finished = float(task.get("agent_finished_at") or 0)
    agent_elapsed = 0
    if agent_started > 0:
        agent_end = agent_finished if agent_finished > 0 else now
        agent_elapsed = int(max(0.0, agent_end - agent_started))
    out = {
        "id": task.get("id"),
        "kind": task.get("kind") or "workspace",
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "elapsed_runtime_sec": workspace_elapsed,
        "owner": task.get("owner"),
        "repo_url": redact_repo_url(str(task.get("repo_url") or "")),
        "source_url": redact_repo_url(str(task.get("source_url") or "")),
        "base_branch": task.get("base_branch"),
        "branch_name": task.get("branch_name"),
        "prompt": task.get("prompt") or "",
        "seed_files": task.get("seed_files") if isinstance(task.get("seed_files"), list) else [],
        "guidance_messages": guidance_messages[-80:],
        "project_plan": normalize_project_plan(task.get("project_plan"), fallback_goal=str(task.get("prompt") or "")),
        "mission": normalize_coding_mission(task),
        "terminal_result": task.get("terminal_result") if isinstance(task.get("terminal_result"), dict) else {},
        "agent_runs": [item for item in run_history[-30:] if isinstance(item, dict)],
        "last_guidance_at": task.get("last_guidance_at"),
        "coding_model": task.get("coding_model") or "",
        "model_policy": coding_model_policy.describe_workspace_model(str(task.get("coding_model") or "")),
        "workspace_path": task.get("workspace_path"),
        "repo_path": task.get("repo_path"),
        "last_command_at": task.get("last_command_at"),
        "last_commit": task.get("last_commit"),
        "last_checkpoint_commit": task.get("last_checkpoint_commit"),
        "last_checkpoint_at": task.get("last_checkpoint_at"),
        "last_checkpoint_cycle": task.get("last_checkpoint_cycle"),
        "last_pushed_at": task.get("last_pushed_at"),
        "last_pr_at": task.get("last_pr_at"),
        "last_pr_output": task.get("last_pr_output"),
        "agent": {
            "run_id": task.get("agent_run_id") or "",
            "status": task.get("agent_status") or "idle",
            "model": task.get("agent_model") or task.get("coding_model") or "",
            "run_prompt": task.get("agent_run_prompt") or "",
            "backend": task.get("agent_backend") or "",
            "upstream_model": task.get("agent_upstream_model") or "",
            "started_at": task.get("agent_started_at"),
            "finished_at": task.get("agent_finished_at"),
            "elapsed_runtime_sec": agent_elapsed,
            "cycle": int(task.get("agent_cycle") or 0),
            "max_cycles": int(task.get("agent_max_cycles") or getattr(S, "CODING_AGENT_MAX_CYCLES_PER_RUN", 1000) or 1000),
            "max_runtime_sec": int(task.get("agent_max_runtime_sec") or getattr(S, "CODING_AGENT_MAX_RUNTIME_SEC", 6 * 60 * 60) or (6 * 60 * 60)),
            "context_reset_cycles": int(task.get("agent_context_reset_cycles") or 0),
            "last_event_at": task.get("agent_last_event_at"),
            "summary": task.get("agent_summary") or "",
            "error": _redact_text(str(task.get("agent_error") or "")),
            "pause_requested": bool(task.get("agent_pause_requested") or task.get("agent_stop_requested")),
            "auto_commit": bool(task.get("agent_auto_commit")),
            "events": agent_events[-80:],
        },
    }
    if isinstance(task.get("integration"), dict):
        out["integration"] = task.get("integration")
    if isinstance(task.get("metadata_error"), dict):
        out["metadata_error"] = task.get("metadata_error")
    if task.get("error"):
        out["error"] = _redact_text(str(task.get("error") or ""))
    if include_commands:
        out["commands"] = task.get("commands") if isinstance(task.get("commands"), list) else []
    return out


def _recent_agent_events(task: Dict[str, Any], *, limit: int = 6) -> List[Dict[str, Any]]:
    events = task.get("agent_events")
    if not isinstance(events, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in events[-max(1, limit):]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "ts": item.get("ts"),
                "type": str(item.get("type") or ""),
                "summary": str(item.get("summary") or item.get("error") or item.get("content") or "")[:400],
                "cycle": int(item.get("cycle") or 0),
            }
        )
    return out


def _recent_agent_event_types(task: Dict[str, Any], *, limit: int = 12) -> set[str]:
    events = task.get("agent_events")
    if not isinstance(events, list):
        return set()
    out: set[str] = set()
    for item in events[-max(1, limit):]:
        if isinstance(item, dict):
            out.add(str(item.get("type") or ""))
    return out


def _no_tool_call_streak(task: Dict[str, Any]) -> int:
    events = task.get("agent_events")
    if not isinstance(events, list):
        return 0
    streak = 0
    for item in reversed(events):
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("type") or "")
        if event_type == "no_tool_call":
            streak += 1
            continue
        if event_type in {"assistant", "tool_started", "tool_finished", "cycle_started", "guidance_seen", "thinking", "started", "queued", "review", "completed", "failed", "paused", "stopped", "checkpoint", "commit"}:
            break
    return streak


def _task_monitor_summary(task: Dict[str, Any], *, stalled_after_sec: float = 900.0, now: Optional[float] = None) -> Dict[str, Any]:
    public = public_task(task, include_commands=False)
    agent = public.get("agent") if isinstance(public.get("agent"), dict) else {}
    task_id = str(public.get("id") or "")
    now_ts = float(now if now is not None else _now())
    agent_status = str(agent.get("status") or "idle")
    last_event_at = float(agent.get("last_event_at") or 0)
    last_event_age_sec = int(max(0.0, now_ts - last_event_at)) if last_event_at > 0 else None
    no_tool_streak = _no_tool_call_streak(task)
    recent_event_types = _recent_agent_event_types(task)
    metadata_error = task.get("metadata_error") if isinstance(task.get("metadata_error"), dict) else None
    pending_summary: Dict[str, Any] = {"ok": False, "counts": {"total": 0}, "files": [], "error": ""}
    workspace_summary: Dict[str, Any] = {"ok": False, "counts": {"total": 0}, "files": [], "error": ""}
    committed_summary: Dict[str, Any] = {"ok": False, "counts": {"total": 0}, "files": [], "error": ""}
    repo_error = ""
    try:
        pending_summary = git_change_summary(task_id)
        repo = _repo_path(task)
        base = _git_base_branch_diff(repo, base_branch=str(task.get("base_branch") or "main"))
        if isinstance(base.get("changes"), dict):
            workspace_summary = base["changes"]
        if isinstance(base.get("committed_changes"), dict):
            committed_summary = base["committed_changes"]
        repo_error = str(base.get("error") or "")
    except Exception as exc:
        repo_error = f"{type(exc).__name__}: {exc}"

    pending_total = int(((pending_summary.get("counts") or {}).get("total") or 0))
    workspace_total = int(((workspace_summary.get("counts") or {}).get("total") or 0))
    committed_total = int(((committed_summary.get("counts") or {}).get("total") or 0))

    attention: List[str] = []
    safe_actions: List[str] = []

    if str(public.get("status") or "") == "error":
        attention.append("workspace_error_state")
    if metadata_error:
        attention.append("metadata_read_failed")
    if agent_status in {"paused", "stopped", "interrupted"}:
        attention.append(f"run_{agent_status}")
        safe_actions.extend(["resume", "guide_and_resume"])
    elif agent_status == "failed":
        attention.append("run_failed")
        safe_actions.extend(["resume", "guide_and_resume"])
    elif agent_status == "running" and last_event_age_sec is not None and last_event_age_sec >= int(max(60.0, stalled_after_sec)):
        attention.append("running_stalled")
        safe_actions.append("guidance")

    if no_tool_streak >= 3 or "no_tool_call_limit" in recent_event_types:
        attention.append("repeated_no_tool_call")
        if agent_status == "running":
            safe_actions.append("guidance")
        else:
            safe_actions.append("guide_and_resume")

    if "no_change_audit" in recent_event_types:
        attention.append("no_change_audit")
    if "finish_gate" in recent_event_types:
        attention.append("finish_gate")

    if "metadata_read_failed" in attention:
        safe_actions = []
    elif {"repeated_no_tool_call", "no_change_audit", "finish_gate"}.intersection(attention):
        safe_actions = [item for item in safe_actions if item != "resume"]
        if agent_status != "running" and "guide_and_resume" not in safe_actions:
            safe_actions.append("guide_and_resume")

    if repo_error:
        attention.append("repo_inspection_error")

    recommended_action = ""
    for candidate in ("guide_and_resume", "resume", "guidance"):
        if candidate in safe_actions:
            recommended_action = candidate
            break

    return {
        "id": task_id,
        "kind": str(public.get("kind") or "workspace"),
        "status": str(public.get("status") or ""),
        "owner": str(public.get("owner") or ""),
        "owner_user_id": task.get("owner_user_id"),
        "repo_url": str(public.get("repo_url") or ""),
        "base_branch": str(public.get("base_branch") or ""),
        "branch_name": str(public.get("branch_name") or ""),
        "prompt": str(public.get("prompt") or "")[:600],
        "workspace_path": str(public.get("workspace_path") or ""),
        "last_checkpoint_commit": str(public.get("last_checkpoint_commit") or ""),
        "last_commit": str(public.get("last_commit") or ""),
        "agent": {
            "status": agent_status,
            "started_at": agent.get("started_at"),
            "finished_at": agent.get("finished_at"),
            "elapsed_runtime_sec": agent.get("elapsed_runtime_sec"),
            "last_event_at": agent.get("last_event_at"),
            "last_event_age_sec": last_event_age_sec,
            "summary": str(agent.get("summary") or "")[:600],
            "error": str(agent.get("error") or "")[:600],
            "model": str(agent.get("model") or ""),
            "backend": str(agent.get("backend") or ""),
            "upstream_model": str(agent.get("upstream_model") or ""),
        },
        "pending_changes": {
            "counts": pending_summary.get("counts") if isinstance(pending_summary.get("counts"), dict) else {"total": pending_total},
            "files": pending_summary.get("files") if isinstance(pending_summary.get("files"), list) else [],
        },
        "workspace_changes": {
            "counts": workspace_summary.get("counts") if isinstance(workspace_summary.get("counts"), dict) else {"total": workspace_total},
            "files": workspace_summary.get("files") if isinstance(workspace_summary.get("files"), list) else [],
        },
        "committed_changes": {
            "counts": committed_summary.get("counts") if isinstance(committed_summary.get("counts"), dict) else {"total": committed_total},
            "files": committed_summary.get("files") if isinstance(committed_summary.get("files"), list) else [],
        },
        "recent_events": _recent_agent_events(task),
        "no_tool_call_streak": no_tool_streak,
        "attention": attention,
        "needs_attention": bool(attention),
        "safe_actions": list(dict.fromkeys(safe_actions)),
        "recommended_action": recommended_action,
        "repo_error": repo_error,
    }


def monitor_tasks(*, limit: int = 20, only_attention: bool = False, stalled_after_sec: float = 900.0) -> Dict[str, Any]:
    _ensure_enabled()
    cleanup_expired_harness_tasks()
    items: List[Dict[str, Any]] = []
    for public in list_tasks(limit=max(1, min(int(limit or 20), 100))):
        task_id = str(public.get("id") or "")
        if not task_id:
            continue
        try:
            task = load_task(task_id)
            item = _task_monitor_summary(task, stalled_after_sec=stalled_after_sec)
            if only_attention and not item.get("needs_attention"):
                continue
            items.append(item)
        except Exception as exc:
            items.append(
                {
                    "id": task_id,
                    "needs_attention": True,
                    "attention": ["monitor_read_failed"],
                    "safe_actions": [],
                    "recommended_action": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "ok": True,
        "tasks": items,
        "counts": {
            "total": len(items),
            "attention": sum(1 for item in items if item.get("needs_attention")),
            "running": sum(1 for item in items if ((item.get("agent") or {}).get("status") == "running")),
            "paused": sum(1 for item in items if ((item.get("agent") or {}).get("status") == "paused")),
            "stopped": sum(1 for item in items if ((item.get("agent") or {}).get("status") == "stopped")),
            "failed": sum(1 for item in items if ((item.get("agent") or {}).get("status") == "failed")),
        },
    }


def inspect_task(task_id: str, *, stalled_after_sec: float = 900.0) -> Dict[str, Any]:
    task = load_task(task_id)
    return {"ok": True, "task": _task_monitor_summary(task, stalled_after_sec=stalled_after_sec)}


def config_payload(*, git_token_value: Optional[str] = None, preferred_coding_model: Optional[str] = None) -> Dict[str, Any]:
    return {
        "enabled": coding_enabled(),
        "bearer_api_enabled": bool(getattr(S, "CODING_ALLOW_BEARER_API", True)),
        "require_admin": bool(getattr(S, "CODING_REQUIRE_ADMIN", True)),
        "workspace_root": str(workspace_root()),
        "tasks_dir": str(tasks_dir()),
        "default_repo_url": redact_repo_url(default_repo_url()),
        "default_base_branch": str(getattr(S, "CODING_DEFAULT_BASE_BRANCH", "") or "main"),
        "allowed_repos": allowed_repos_public(),
        "allowed_commands": allowed_commands(),
        "command_timeout_sec": command_timeout_sec(),
        "max_output_chars": max_output_chars(),
        "file_max_bytes": file_max_bytes(),
        "agent_max_tokens": int(getattr(S, "CODING_AGENT_MAX_TOKENS", 8192) or 8192),
        "agent_tool_context_chars": int(getattr(S, "CODING_AGENT_TOOL_CONTEXT_CHARS", 32_000) or 32_000),
        "agent_max_cycles_per_run": int(getattr(S, "CODING_AGENT_MAX_CYCLES_PER_RUN", 1000) or 1000),
        "agent_max_runtime_sec": int(getattr(S, "CODING_AGENT_MAX_RUNTIME_SEC", 6 * 60 * 60) or (6 * 60 * 60)),
        "agent_context_reset_cycles": int(getattr(S, "CODING_AGENT_CONTEXT_RESET_CYCLES", 0) or 0),
        "agent_context_reset_chars": int(getattr(S, "CODING_AGENT_CONTEXT_RESET_CHARS", 64_000) or 64_000),
        "agent_run_history_limit": int(getattr(S, "CODING_AGENT_RUN_HISTORY_LIMIT", 50) or 50),
        "agent_checkpoint_commits": bool(getattr(S, "CODING_AGENT_CHECKPOINT_COMMITS", True)),
        "git_token_configured": bool(_effective_git_token(git_token_value)),
        "preferred_coding_model": str(preferred_coding_model or "").strip(),
        "gh_cli_available": shutil.which("gh") is not None,
        "model_integration_runtimes": ["auto", "mlx", "vllm", "transformers", "diffusers", "custom"],
        "model_integration_route_kinds": ["chat", "embeddings", "images", "tts", "ocr", "video", "music", "json"],
        "model_integration_host_lanes": miw.integration_host_lanes(),
    }

def _serialize_task_workspace_operation(operation: Any) -> Any:
    """Make a task-scoped checkout operation participate in finalization serialization."""
    if bool(getattr(operation, "_nexus_workspace_serialized", False)):
        return operation

    def serialized(task_id: str, *args: Any, **kwargs: Any) -> Any:
        with task_workspace_lock(task_id):
            return operation(task_id, *args, **kwargs)

    serialized.__name__ = str(getattr(operation, "__name__", "serialized_workspace_operation"))
    serialized.__doc__ = getattr(operation, "__doc__", None)
    serialized._nexus_workspace_serialized = True
    serialized._nexus_workspace_operation = operation
    return serialized


def ensure_task_workspace_serialized(operation_name: str) -> Any:
    """Keep serialization outermost when later installers replace an operation."""
    name = str(operation_name or "").strip()
    operation = globals().get(name)
    if not callable(operation):
        return operation
    wrapped = _serialize_task_workspace_operation(operation)
    globals()[name] = wrapped
    return wrapped


# These operations can mutate, remove, commit, or externally publish the task
# checkout. Keeping them on one re-entrant task lock lets finalization hold the
# checkout stable from semantic-acceptance revalidation through its final side
# effect without deadlocking nested commit/push/PR calls.
for _workspace_operation_name in (
    "write_file",
    "replace_text",
    "apply_unified_patch",
    "run_task_command",
    "checkpoint_task",
    "commit_task",
    "push_task",
    "create_pull_request",
    "archive_task",
    "delete_task",
):
    _workspace_operation = globals().get(_workspace_operation_name)
    if callable(_workspace_operation):
        globals()[_workspace_operation_name] = _serialize_task_workspace_operation(
            _workspace_operation
        )
