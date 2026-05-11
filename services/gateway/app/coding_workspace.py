from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from app.config import S, logger
from app import model_integration_workspace as miw


SCHEMA = "nexus_coding_task.v1"
_SAFE_TASK_RE = re.compile(r"^code_[a-f0-9]{12}$")
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


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="coding task not found")
    except Exception as exc:
        logger.warning("coding task read failed path=%s error=%s", path, exc)
        raise HTTPException(status_code=500, detail="coding task metadata is unreadable")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise HTTPException(status_code=500, detail="coding task metadata is invalid")
    return data


def load_task(task_id: str) -> Dict[str, Any]:
    _ensure_enabled()
    return _read_json(_task_path(task_id))


def save_task(task: Dict[str, Any]) -> Dict[str, Any]:
    task["updated_at"] = _now()
    _write_json(_task_path(str(task.get("id") or "")), task)
    return task


def append_guidance_message(
    task_id: str,
    *,
    message: str,
    actor: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    task = load_task(task_id)
    text = str(message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")
    messages = task.get("guidance_messages")
    if not isinstance(messages, list):
        messages = []
    messages.append(
        {
            "ts": _now(),
            "role": "user",
            "actor": str(actor or "").strip(),
            "run_id": str(run_id or "").strip(),
            "content": text,
        }
    )
    task["guidance_messages"] = messages[-200:]
    task["last_guidance_at"] = _now()
    save_task(task)
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
        if status not in {"queued", "running", "stopping"}:
            continue
        events = task.get("agent_events")
        if not isinstance(events, list):
            events = []
        ev = {
            "ts": _now(),
            "type": "interrupted",
            "summary": (
                "Gateway restarted while this coding run was active. "
                "Start another run on the same workspace to continue from the latest checkpoint commit and current git state."
            ),
            "previous_status": status,
            "run_id": task.get("agent_run_id") or "",
        }
        events.append(ev)
        task["agent_events"] = events[-max(20, min(int(getattr(S, "CODING_AGENT_MAX_EVENTS", 120) or 120), 1000)) :]
        task["agent_previous_status"] = status
        task["agent_status"] = "interrupted"
        task["agent_summary"] = ev["summary"]
        task["agent_error"] = ev["summary"]
        task["agent_finished_at"] = _now()
        task["agent_last_event_at"] = ev["ts"]
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
    repo = str(repo_url or "").strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo_url is required for model integration workspaces")
    _reject_url_credentials(repo)
    if not _is_github_url(repo):
        raise HTTPException(status_code=400, detail="model integration repo_url must be a GitHub repository URL")
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


def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_sec: Optional[float] = None,
    use_git_credentials: bool = False,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    limit = max_output_chars()
    env = _base_env()
    redaction_tokens = [_effective_git_token(git_token_value)] if use_git_credentials else []
    effective_argv = _argv_with_git_safe_directory(argv, cwd=cwd)
    with _GitCredentialEnv(use_git_credentials, git_token_value=git_token_value) as extra_env:
        env.update(extra_env)
        try:
            proc = subprocess.run(
                effective_argv,
                cwd=str(cwd),
                env=env,
                text=True,
                capture_output=True,
                timeout=command_timeout_sec(timeout_sec),
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
                "stderr": stderr or f"timeout after {command_timeout_sec(timeout_sec):.0f}s",
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
    }
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
) -> Dict[str, Any]:
    _ensure_enabled()
    _ensure_dirs()
    plan = miw.build_integration_plan(
        model=model,
        preferred_runtime=preferred_runtime,
        route_kind=route_kind,
        service_name=service_name,
        prompt=prompt,
    )
    target_repo = _resolve_model_integration_repo_url(repo_url)
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
        "commands": [],
    }
    save_task(task)

    try:
        workspace.mkdir(parents=True, exist_ok=True)
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
        ):
            save_task(task)
            return public_task(task)

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


def _resolve_repo_child(task: Dict[str, Any], rel_path: Optional[str] = None) -> Path:
    base = _repo_path(task)
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
        value = item.strip()
        if not value:
            raise HTTPException(status_code=400, detail="argv entries must be non-empty")
        if "\x00" in value:
            raise HTTPException(status_code=400, detail="argv entries cannot contain NUL")
        if len(value) > 4096:
            raise HTTPException(status_code=400, detail="argv entry is too long")
        out.append(value)
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
    return out


def run_task_command(
    task_id: str,
    *,
    argv: Sequence[str],
    cwd: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    git_token_value: Optional[str] = None,
) -> Dict[str, Any]:
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
    _append_command(task, result, label="command")
    save_task(task)
    return result


def git_status(task_id: str, *, git_token_value: Optional[str] = None) -> Dict[str, Any]:
    result = run_task_command(task_id, argv=["git", "status", "--short", "--branch"], git_token_value=git_token_value)
    return result


def git_head(task_id: str) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    result = _run_process(["git", "rev-parse", "HEAD"], cwd=repo)
    return {"ok": bool(result.get("ok")), "commit": str(result.get("stdout") or "").strip(), "raw": result}


def git_change_summary(task_id: str) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    result = _run_process(["git", "status", "--porcelain"], cwd=repo)
    counts = {"added": 0, "modified": 0, "removed": 0, "renamed": 0, "untracked": 0, "other": 0, "total": 0}
    files: List[Dict[str, Any]] = []
    if result.get("ok"):
        for raw_line in str(result.get("stdout") or "").splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            code = line[:2]
            path = line[3:] if len(line) > 3 else ""
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
            files.append({"path": path, "status": code, "kind": kind})
    return {"ok": bool(result.get("ok")), "counts": counts, "files": files[:500], "truncated": len(files) > 500, "raw": result}


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


def _git_name_status_summary(repo: Path, argv: Sequence[str]) -> Dict[str, Any]:
    result = _run_process(list(argv), cwd=repo)
    files: List[Dict[str, Any]] = []
    if result.get("ok"):
        for raw_line in str(result.get("stdout") or "").splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            parts = line.split("\t")
            status = str(parts[0] if parts else "").strip()
            kind = _diff_kind_from_status(status)
            path = str(parts[-1] if len(parts) >= 2 else "").strip()
            previous_path = str(parts[1] if len(parts) >= 3 else "").strip() or None
            files.append(
                {
                    "path": path,
                    "previous_path": previous_path,
                    "status": status,
                    "kind": kind,
                }
            )
    return {
        "ok": bool(result.get("ok")),
        "counts": _counts_for_change_files(files),
        "files": files[:500],
        "truncated": len(files) > 500,
        "raw": result,
    }


def _git_ref_exists(repo: Path, ref: str) -> bool:
    candidate = str(ref or "").strip()
    if not candidate:
        return False
    result = _run_process(["git", "rev-parse", "--verify", candidate], cwd=repo)
    return bool(result.get("ok"))


def _git_base_branch_diff(repo: Path, *, base_branch: str) -> Dict[str, Any]:
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
    workspace_changes = _git_name_status_summary(repo, ["git", "diff", "--name-status", compare_ref, "--"])
    committed_stat = _run_process(["git", "diff", "--stat", compare_ref, "HEAD", "--"], cwd=repo)
    committed_diff = _run_process(["git", "diff", compare_ref, "HEAD", "--"], cwd=repo)
    committed_changes = _git_name_status_summary(repo, ["git", "diff", "--name-status", compare_ref, "HEAD", "--"])
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


def apply_unified_patch(task_id: str, *, patch: str, check_only: bool = False) -> Dict[str, Any]:
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


def git_diff(task_id: str) -> Dict[str, Any]:
    task = load_task(task_id)
    repo = _repo_path(task)
    branch = str(task.get("branch_name") or "").strip()
    base_branch = str(task.get("base_branch") or "main").strip()
    base_diff = _git_base_branch_diff(repo, base_branch=base_branch)
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


def commit_task(task_id: str, *, message: str) -> Dict[str, Any]:
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


def checkpoint_task(task_id: str, *, message: str, run_id: Optional[str] = None, turn: Optional[int] = None) -> Dict[str, Any]:
    msg = str(message or "").strip() or "Nexus coding agent checkpoint"
    if len(msg) > 2000:
        msg = msg[:2000]
    task = load_task(task_id)
    repo = _repo_path(task)
    status = _run_process(["git", "status", "--porcelain"], cwd=repo)
    if not status.get("ok"):
        _append_command(task, status, label="checkpoint-status")
        save_task(task)
        return {"ok": False, "changed": False, "status": status, "error": "git status failed"}
    if not str(status.get("stdout") or "").strip():
        save_task(task)
        return {"ok": True, "changed": False, "status": status, "message": "no changes to checkpoint"}
    add = _run_process(["git", "add", "-A"], cwd=repo)
    _append_command(task, add, label="checkpoint-add")
    if not add.get("ok"):
        save_task(task)
        return {"ok": False, "changed": True, "add": add, "error": "git add failed"}
    commit = _run_process(["git", "commit", "-m", msg], cwd=repo)
    _append_command(task, commit, label="checkpoint-commit")
    rev = {"ok": False, "stdout": ""}
    if commit.get("ok"):
        rev = _run_process(["git", "rev-parse", "HEAD"], cwd=repo)
        commit_hash = str(rev.get("stdout") or "").strip()
        task["last_commit"] = commit_hash
        task["last_checkpoint_commit"] = commit_hash
        task["last_checkpoint_at"] = _now()
        task["last_checkpoint_run_id"] = str(run_id or "").strip()
        task["last_checkpoint_turn"] = int(turn or 0)
    save_task(task)
    return {
        "ok": bool(commit.get("ok")),
        "changed": True,
        "status": status,
        "add": add,
        "commit": commit,
        "rev": rev,
        "last_commit": task.get("last_commit"),
    }


def push_task(task_id: str, *, remote: Optional[str] = None, git_token_value: Optional[str] = None) -> Dict[str, Any]:
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


def _attach_model_integration_remote(
    task: Dict[str, Any],
    *,
    repo: Path,
    repo_url: str,
    base_branch: str,
    branch_name: str,
    git_token_value: Optional[str] = None,
) -> bool:
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


def delete_task(task_id: str) -> Dict[str, Any]:
    task = load_task(task_id)
    path = _task_workspace(task_id)
    root = workspace_root()
    _ensure_inside(root, path)
    if path.exists():
        shutil.rmtree(path)
    meta = _task_path(task_id)
    try:
        meta.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "task_id": task_id, "deleted_workspace": str(path), "repo_url": redact_repo_url(str(task.get("repo_url") or ""))}


def agent_brief(task_id: str, *, coding_model: Optional[str] = None) -> Dict[str, Any]:
    task = load_task(task_id)
    task_public = public_task(task)
    prompt = str(task.get("prompt") or "").strip() or "(no task prompt recorded)"
    branch = str(task.get("branch_name") or "").strip()
    base = str(task.get("base_branch") or "").strip()
    model = str(coding_model or task.get("coding_model") or "").strip()
    integration = task.get("integration") if isinstance(task.get("integration"), dict) else None
    if str(task.get("kind") or "") == "model_integration" and integration is not None:
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

Use the Nexus Coding API for workspace operations. Prefer a tight loop:
1. Review README.md, AGENT_TASK.md, and integration_request.json.
2. Fill in the generated scaffold under services/ or host_native/.
3. Update integration/backend-config-snippet.yaml and integration/lifecycle.backend.json.
4. Run targeted checks with POST /v1/coding/tasks/{task.get("id")}/command.
5. Review GET /v1/coding/tasks/{task.get("id")}/diff.

Constraints:
- Work only inside this task workspace repo.
- Keep the resulting backend compatible with the expected OpenAI-style route.
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
- Do not edit unrelated files or force-push.
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
    out = {
        "id": task.get("id"),
        "kind": task.get("kind") or "workspace",
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "owner": task.get("owner"),
        "repo_url": redact_repo_url(str(task.get("repo_url") or "")),
        "source_url": redact_repo_url(str(task.get("source_url") or "")),
        "base_branch": task.get("base_branch"),
        "branch_name": task.get("branch_name"),
        "prompt": task.get("prompt") or "",
        "guidance_messages": guidance_messages[-80:],
        "last_guidance_at": task.get("last_guidance_at"),
        "coding_model": task.get("coding_model") or "",
        "workspace_path": task.get("workspace_path"),
        "repo_path": task.get("repo_path"),
        "last_command_at": task.get("last_command_at"),
        "last_commit": task.get("last_commit"),
        "last_checkpoint_commit": task.get("last_checkpoint_commit"),
        "last_checkpoint_at": task.get("last_checkpoint_at"),
        "last_checkpoint_turn": task.get("last_checkpoint_turn"),
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
            "turn": int(task.get("agent_turn") or 0),
            "max_turns": int(task.get("agent_max_turns") or 0),
            "started_at": task.get("agent_started_at"),
            "finished_at": task.get("agent_finished_at"),
            "last_event_at": task.get("agent_last_event_at"),
            "summary": task.get("agent_summary") or "",
            "error": _redact_text(str(task.get("agent_error") or "")),
            "stop_requested": bool(task.get("agent_stop_requested")),
            "auto_commit": bool(task.get("agent_auto_commit")),
            "events": agent_events[-80:],
        },
    }
    if isinstance(task.get("integration"), dict):
        out["integration"] = task.get("integration")
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
                "turn": int(item.get("turn") or 0),
            }
        )
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
        if event_type in {"assistant", "tool_started", "tool_finished", "turn_started", "guidance_seen", "thinking", "started", "queued", "review", "completed", "failed", "stopped", "checkpoint", "commit"}:
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
    if agent_status in {"stopped", "interrupted"}:
        attention.append(f"run_{agent_status}")
        safe_actions.extend(["resume", "guide_and_resume"])
    elif agent_status == "failed":
        attention.append("run_failed")
        safe_actions.extend(["resume", "guide_and_resume"])
    elif agent_status == "running" and last_event_age_sec is not None and last_event_age_sec >= int(max(60.0, stalled_after_sec)):
        attention.append("running_stalled")
        safe_actions.append("guidance")

    if no_tool_streak >= 3:
        attention.append("repeated_no_tool_call")
        if agent_status == "running":
            safe_actions.append("guidance")
        else:
            safe_actions.append("guide_and_resume")

    max_turns = int(agent.get("max_turns") or 0)
    turn = int(agent.get("turn") or 0)
    if max_turns > 0 and turn >= max(1, int(max_turns * 0.9)) and agent_status == "running":
        attention.append("near_turn_limit")

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
        "repo_url": str(public.get("repo_url") or ""),
        "base_branch": str(public.get("base_branch") or ""),
        "branch_name": str(public.get("branch_name") or ""),
        "prompt": str(public.get("prompt") or "")[:600],
        "workspace_path": str(public.get("workspace_path") or ""),
        "last_checkpoint_commit": str(public.get("last_checkpoint_commit") or ""),
        "last_commit": str(public.get("last_commit") or ""),
        "agent": {
            "status": agent_status,
            "turn": turn,
            "max_turns": max_turns,
            "started_at": agent.get("started_at"),
            "finished_at": agent.get("finished_at"),
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
        "agent_max_turns": int(getattr(S, "CODING_AGENT_MAX_TURNS", 1000) or 1000),
        "agent_max_turns_limit": int(getattr(S, "CODING_AGENT_MAX_TURNS_LIMIT", 10_000) or 10_000),
        "agent_max_runtime_sec": int(getattr(S, "CODING_AGENT_MAX_RUNTIME_SEC", 0) or 0),
        "agent_max_tokens": int(getattr(S, "CODING_AGENT_MAX_TOKENS", 512) or 512),
        "agent_tool_context_chars": int(getattr(S, "CODING_AGENT_TOOL_CONTEXT_CHARS", 10_000) or 10_000),
        "agent_checkpoint_commits": bool(getattr(S, "CODING_AGENT_CHECKPOINT_COMMITS", True)),
        "git_token_configured": bool(_effective_git_token(git_token_value)),
        "preferred_coding_model": str(preferred_coding_model or "").strip(),
        "gh_cli_available": shutil.which("gh") is not None,
        "model_integration_runtimes": ["auto", "mlx", "vllm", "transformers"],
        "model_integration_route_kinds": ["chat", "embeddings", "images", "tts", "ocr", "video", "music", "json"],
        "model_integration_host_lanes": miw.integration_host_lanes(),
    }
