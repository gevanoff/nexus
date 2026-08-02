from __future__ import annotations

import asyncio
import fnmatch
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from app.agent_api.auth import AgentToolCaller
from app.agent_api.constants import OPERATIONS as AGENT_API_OPERATIONS
from app.agent_api.tool import execute_agent_api_tool
from app.config import S
from app.model_aliases import get_aliases, get_aliases_state
from app.resources_snapshot import build_resources_snapshot
from app.tool_calling.capabilities import tool_calling_diagnostics
from app.tool_calling.schemas import strict_object_schema


ToolImplementation = Callable[..., Awaitable[dict[str, Any]]]
_SECRET_RE = re.compile(r"(?i)\b(authorization|bearer|api[_-]?key|password|secret|token|cookie)\b(\s*[:=]\s*)(bearer\s+)?([^\s,;]+)")
_BING_RESULT_RE = re.compile(
    r'<li\b[^>]*class=["\'][^"\']*\bb_algo\b[^"\']*["\'][^>]*>(.*?)</li>',
    re.IGNORECASE | re.DOTALL,
)
_BING_TITLE_RE = re.compile(
    r'<h2\b[^>]*>.*?<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>.*?</h2>',
    re.IGNORECASE | re.DOTALL,
)
_BING_SNIPPET_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class NexusToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    toolset: str
    risk: str = "read_only"
    default_enabled: bool = True
    timeout_sec: float = 20.0
    output_limit: int = 12000
    implementation: ToolImplementation | None = None
    uses_caller_context: bool = False

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
                "strict": True,
            },
        }


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{match.group(3) or ''}[REDACTED]", value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if re.search(r"(?i)token|authorization|api[_-]?key|password|secret|cookie", str(key)) else redact_secrets(item))
            for key, item in value.items()
        }
    return value


def _csv(value: str) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _allowed_roots() -> list[Path]:
    return [Path(item).expanduser().resolve() for item in _csv(S.NEXUS_TOOL_FS_ROOTS)]


def _resolve_path(raw: str, *, require_dir: bool | None = None) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path is required")
    candidates = [Path(value)] if Path(value).is_absolute() else [root / value for root in _allowed_roots()]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if not any(resolved == root or root in resolved.parents for root in _allowed_roots()):
            continue
        if not resolved.exists():
            continue
        if require_dir is True and not resolved.is_dir():
            raise ValueError("path must be a directory")
        if require_dir is False and not resolved.is_file():
            raise ValueError("path must be a file")
        return resolved
    raise ValueError("path is outside allowlisted roots or does not exist")


async def _health(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "gateway": "healthy"}
    if args["include_models"]:
        out["aliases"] = sorted(get_aliases())
    if args["include_upstreams"]:
        snapshot = await build_resources_snapshot(refresh_lifecycle=False)
        resources = snapshot.get("resources") if isinstance(snapshot, dict) else {}
        out["upstreams"] = (resources or {}).get("backends", [])
    return out


async def _models(args: dict[str, Any]) -> dict[str, Any]:
    aliases = []
    diagnostics = {item["alias"]: item for item in tool_calling_diagnostics()}
    for name, alias in sorted(get_aliases().items()):
        item = {"alias": name, "backend": alias.backend, "model": alias.upstream_model, "context_window": alias.context_window}
        if args["include_capabilities"]:
            item["capabilities"] = diagnostics.get(name, {})
        aliases.append(item)
    return {"ok": True, "aliases": aliases, "source": get_aliases_state().source}


async def _alias(args: dict[str, Any]) -> dict[str, Any]:
    name = args["alias"]
    alias = get_aliases().get(name)
    if alias is None:
        return {"ok": False, "error": "unknown_alias", "alias": name}
    item = next((row for row in tool_calling_diagnostics() if row["alias"] == name), {})
    return {"ok": True, **item, "context_window": alias.context_window}


async def _diagnostics(_args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "aliases": tool_calling_diagnostics()}


def _plain_html_text(value: str) -> str:
    return " ".join(html.unescape(_HTML_TAG_RE.sub(" ", value or "")).split())


def _parse_bing_results(page: str, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for block in _BING_RESULT_RE.findall(page or ""):
        title_match = _BING_TITLE_RE.search(block)
        if title_match is None:
            continue
        url = html.unescape(title_match.group(1)).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        title = _plain_html_text(title_match.group(2))[:500]
        if not title:
            continue
        snippet_match = _BING_SNIPPET_RE.search(block)
        snippet = _plain_html_text(snippet_match.group(1))[:1000] if snippet_match else ""
        results.append({"title": title, "url": url[:2000], "snippet": snippet})
        if len(results) >= limit:
            break
    return results


async def _web_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query or len(query) > 500:
        return {"ok": False, "error": "query must contain 1-500 characters"}
    limit = int(args.get("limit") or 5)
    if limit < 1 or limit > 10:
        return {"ok": False, "error": "limit must be between 1 and 10"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        response = await client.get("https://www.bing.com/search", params={"q": query})
        response.raise_for_status()
    return {
        "ok": True,
        "query": query,
        "provider": "bing",
        "results": _parse_bing_results(response.text, limit),
    }


async def _file_list(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_path(args["path"], require_dir=True)
    limit = args["limit"]
    max_depth = args["max_depth"]
    rows: list[dict[str, Any]] = []
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        if depth >= max_depth:
            dirs[:] = []
        for name in sorted([*dirs, *files]):
            path = current_path / name
            rows.append({"path": str(path.relative_to(root)), "type": "directory" if path.is_dir() else "file"})
            if len(rows) >= limit:
                return {"ok": True, "root": str(root), "entries": rows, "truncated": True}
    return {"ok": True, "root": str(root), "entries": rows, "truncated": False}


async def _file_read(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_path(args["path"], require_dir=False)
    if path.stat().st_size > int(args["max_chars"]) * 8:
        return {"ok": False, "error": "file_too_large", "path": str(path)}
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        return {"ok": False, "error": "binary_file_rejected", "path": str(path)}
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = args["start_line"] or 1
    end = args["end_line"] or len(lines)
    if end < start:
        return {"ok": False, "error": "end_line_before_start_line"}
    content = "\n".join(lines[start - 1 : end])
    max_chars = args["max_chars"]
    truncated = len(content) > max_chars
    return {"ok": True, "path": str(path), "start_line": start, "end_line": min(end, len(lines)), "content": redact_secrets(content[:max_chars]), "truncated": truncated}


async def _file_stat(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_path(args["path"])
    stat = path.stat()
    return {
        "ok": True,
        "path": str(path),
        "type": "directory" if path.is_dir() else "file",
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


async def _file_grep(args: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_path(args["root"], require_dir=True)
    pattern = str(args["pattern"])
    if not pattern or len(pattern) > 500:
        return {"ok": False, "error": "pattern must contain 1-500 characters"}
    try:
        matcher = re.compile(pattern)
    except re.error as exc:
        return {"ok": False, "error": "invalid_regex", "detail": str(exc)[:300]}
    glob = args["glob"]
    limit = args["limit"]
    matches: list[dict[str, Any]] = []
    scanned = 0
    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if not path.is_file() or (glob and not fnmatch.fnmatchcase(relative_path.as_posix(), glob)):
            continue
        scanned += 1
        if scanned > 2000:
            break
        try:
            if path.stat().st_size > 2_000_000:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if matcher.search(line):
                    matches.append({"path": relative_path.as_posix(), "line": number, "text": redact_secrets(line[:500])})
                    if len(matches) >= limit:
                        return {"ok": True, "matches": matches, "truncated": True}
        except OSError:
            continue
    return {"ok": True, "matches": matches, "truncated": scanned > 2000}


def _git(args: list[str], repo: Path, max_chars: int) -> dict[str, Any]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20, check=False)
    text = redact_secrets((result.stdout or "") + (result.stderr or ""))
    return {"ok": result.returncode == 0, "exit_code": result.returncode, "output": text[:max_chars], "truncated": len(text) > max_chars}


async def _git_status(args: dict[str, Any]) -> dict[str, Any]:
    repo = _resolve_path(args["repo"], require_dir=True)
    return await asyncio.to_thread(_git, ["status", "--short"] if args["short"] else ["status"], repo, 20000)


async def _git_diff(args: dict[str, Any]) -> dict[str, Any]:
    repo = _resolve_path(args["repo"], require_dir=True)
    command = ["diff"]
    if args["cached"]:
        command.append("--cached")
    if args["path"]:
        command.extend(["--", args["path"]])
    return await asyncio.to_thread(_git, command, repo, args["max_chars"])


def _git_log_command(repo: Path, *, limit: int, path: str | None) -> list[str]:
    command = [
        "git",
        "-C",
        str(repo),
        "log",
        f"--max-count={limit}",
        "--date=iso-strict",
        "--format=%H%x1f%h%x1f%aI%x1f%an%x1f%s%x1e",
    ]
    if path:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("path must be relative to the repository")
        command.extend(["--", path])
    return command


def _git_log_result(stdout: str, stderr: str, returncode: int) -> dict[str, Any]:
    commits: list[dict[str, Any]] = []
    for record in stdout.split("\x1e"):
        fields = record.strip().split("\x1f")
        if len(fields) != 5:
            continue
        commits.append(
            {
                "commit": fields[0],
                "short_commit": fields[1],
                "authored_at": fields[2],
                "author": redact_secrets(fields[3]),
                "subject": redact_secrets(fields[4]),
            }
        )
    return {
        "ok": returncode == 0,
        "exit_code": returncode,
        "commits": commits,
        "error": redact_secrets(stderr.strip()) if returncode else None,
    }


def _run_git_log(command: list[str]) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            result = subprocess.run(command, stdout=stdout_file, stderr=stderr_file, timeout=20, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git log timed out"}
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    return _git_log_result(stdout, stderr, result.returncode)


async def _git_log(args: dict[str, Any]) -> dict[str, Any]:
    repo = _resolve_path(args["repo"], require_dir=True)
    try:
        command = _git_log_command(repo, limit=args["limit"], path=args["path"])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if os.name == "nt":
        return _run_git_log(command)
    return await asyncio.to_thread(_run_git_log, command)


async def _resources(args: dict[str, Any]) -> dict[str, Any]:
    snapshot = await build_resources_snapshot(refresh_lifecycle=args["scope"] == "cluster")
    if args["scope"] == "local" and isinstance(snapshot.get("resources"), dict):
        snapshot["resources"] = {"control_plane": snapshot["resources"].get("control_plane", [])}
    return snapshot


async def _docker_ps(args: dict[str, Any]) -> dict[str, Any]:
    if not shutil.which("docker"):
        return {"ok": False, "error": "docker_cli_unavailable"}
    command = ["docker", "ps"] + (["--all"] if args["all"] else []) + ["--format", "{{json .}}"]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10, check=False)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return {"ok": result.returncode == 0, "containers": rows}


async def _docker_logs(args: dict[str, Any]) -> dict[str, Any]:
    if args["container"] not in _csv(S.NEXUS_TOOL_CONTAINERS):
        return {"ok": False, "error": "container_not_allowed"}
    if not shutil.which("docker"):
        return {"ok": False, "error": "docker_cli_unavailable"}
    command = ["docker", "logs", "--tail", str(args["tail"])]
    if args["since"]:
        command.extend(["--since", args["since"]])
    command.append(args["container"])
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10, check=False)
    output = redact_secrets((result.stdout or "") + (result.stderr or ""))
    return {"ok": result.returncode == 0, "output": output}


async def _service_status(args: dict[str, Any]) -> dict[str, Any]:
    if args["service"] not in _csv(S.NEXUS_TOOL_SERVICES):
        return {"ok": False, "error": "service_not_allowed"}
    snapshot = await build_resources_snapshot(refresh_lifecycle=False)
    resources = snapshot.get("resources") if isinstance(snapshot, dict) else {}
    rows = [*(resources or {}).get("core_services", []), *(resources or {}).get("control_plane", [])]
    needle = args["service"].replace("-", "_")
    found = [row for row in rows if needle in json.dumps(row).lower().replace("-", "_")]
    return {"ok": bool(found), "service": args["service"], "status": found[:5]}


async def _http_request(args: dict[str, Any]) -> dict[str, Any]:
    parsed = urlsplit(args["url"])
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "") not in _csv(S.NEXUS_TOOL_HTTP_HOSTS) or str(port) not in _csv(S.NEXUS_TOOL_HTTP_PORTS):
        return {"ok": False, "error": "url_not_allowed"}
    async with httpx.AsyncClient(timeout=args["timeout_seconds"], follow_redirects=False) as client:
        response = await client.request(args["method"], args["url"], json=args["json_body"])
    text = redact_secrets(response.text)
    return {"ok": response.is_success, "status": response.status_code, "body": text[: args["max_chars"]], "truncated": len(text) > args["max_chars"]}


async def _agent_api(args: dict[str, Any], caller: AgentToolCaller | None) -> dict[str, Any]:
    return await execute_agent_api_tool(args, caller)


def _props(**items: Any) -> dict[str, Any]:
    return strict_object_schema(items)


_DEFINITIONS = [
    NexusToolDefinition("nexus_health", "Inspect Gateway and configured upstream health.", _props(include_upstreams={"type": "boolean"}, include_models={"type": "boolean"}), "core", implementation=_health),
    NexusToolDefinition("nexus_models_list", "List configured aliases and served model capabilities.", _props(include_capabilities={"type": "boolean"}), "core", implementation=_models),
    NexusToolDefinition("nexus_alias_resolve", "Resolve one model alias and its tool capabilities.", _props(alias={"type": "string"}), "core", implementation=_alias),
    NexusToolDefinition("nexus_tool_diagnostics", "Report provider-neutral tool-calling capabilities for every alias.", _props(), "core", implementation=_diagnostics),
    NexusToolDefinition(
        "web_search",
        "Search the public web for current information and return titles, URLs, and short snippets.",
        _props(
            query={"type": "string", "minLength": 1, "maxLength": 500},
            limit={"type": "integer", "minimum": 1, "maximum": 10},
        ),
        "web",
        timeout_sec=20.0,
        output_limit=20000,
        implementation=_web_search,
    ),
    NexusToolDefinition("nexus_file_list", "List bounded files under allowlisted Nexus roots.", _props(path={"type": "string"}, max_depth={"type": "integer", "minimum": 0, "maximum": 5}, limit={"type": "integer", "minimum": 1, "maximum": 200}), "repo", implementation=_file_list),
    NexusToolDefinition("nexus_file_read", "Read a bounded text file under allowlisted Nexus roots.", _props(path={"type": "string"}, start_line={"type": ["integer", "null"], "minimum": 1}, end_line={"type": ["integer", "null"], "minimum": 1}, max_chars={"type": "integer", "minimum": 1, "maximum": 50000}), "repo", output_limit=50000, implementation=_file_read),
    NexusToolDefinition("nexus_file_stat", "Inspect metadata for a file or directory under allowlisted Nexus roots.", _props(path={"type": "string"}), "repo", implementation=_file_stat),
    NexusToolDefinition("nexus_file_grep", "Search text under an allowlisted Nexus root.", _props(root={"type": "string"}, pattern={"type": "string"}, glob={"type": ["string", "null"]}, limit={"type": "integer", "minimum": 1, "maximum": 200}), "repo", implementation=_file_grep),
    NexusToolDefinition("nexus_git_status", "Inspect an allowlisted Nexus repository status.", _props(repo={"type": "string"}, short={"type": "boolean"}), "repo", implementation=_git_status),
    NexusToolDefinition("nexus_git_diff", "Read a bounded diff from an allowlisted Nexus repository.", _props(repo={"type": "string"}, path={"type": ["string", "null"]}, cached={"type": "boolean"}, max_chars={"type": "integer", "minimum": 1, "maximum": 60000}), "repo", output_limit=60000, implementation=_git_diff),
    NexusToolDefinition("nexus_git_log", "Read structured recent history from an allowlisted Nexus repository.", _props(repo={"type": "string"}, path={"type": ["string", "null"]}, limit={"type": "integer", "minimum": 1, "maximum": 50}), "repo", implementation=_git_log),
    NexusToolDefinition("nexus_resources_snapshot", "Return a bounded Nexus hardware and service resource snapshot.", _props(scope={"type": "string", "enum": ["local", "cluster"]}), "ops", implementation=_resources),
    NexusToolDefinition("nexus_docker_ps", "List Nexus containers without exposing environment values.", _props(all={"type": "boolean"}), "ops", implementation=_docker_ps),
    NexusToolDefinition("nexus_docker_logs", "Tail bounded, redacted logs from allowlisted Nexus containers.", _props(container={"type": "string"}, tail={"type": "integer", "minimum": 1, "maximum": 500}, since={"type": ["string", "null"]}), "ops", implementation=_docker_logs),
    NexusToolDefinition("nexus_service_status", "Inspect one allowlisted Nexus service.", _props(service={"type": "string"}), "ops", implementation=_service_status),
    NexusToolDefinition("nexus_http_request", "Perform a bounded HTTP request to an allowlisted internal Nexus endpoint.", _props(method={"type": "string", "enum": ["GET", "POST"]}, url={"type": "string"}, json_body={"type": ["object", "null"]}, timeout_seconds={"type": "integer", "minimum": 1, "maximum": 20}, max_chars={"type": "integer", "minimum": 1, "maximum": 50000}), "ops", output_limit=50000, implementation=_http_request),
    NexusToolDefinition(
        "nexus_agent_api",
        (
            "Use the authenticated Nexus Agent API to manage coding workspaces, tasks, execution, and artifacts. "
            "Set workspace_id and task_id to null when the selected operation does not use them. Parameters mirror "
            "the REST request or query fields; use an empty object when none are needed. Binary artifact content is base64."
        ),
        _props(
            operation={"type": "string", "enum": list(AGENT_API_OPERATIONS), "description": "Agent API operation to perform."},
            workspace_id={"type": ["string", "null"], "description": "Workspace id for workspace-scoped operations; otherwise null."},
            task_id={"type": ["string", "null"], "description": "Task id for get, update, delete, and retry task operations; otherwise null."},
            parameters={
                "type": "object",
                "description": (
                    "Operation fields: create_workspace uses name, description, metadata; update_workspace uses changed fields; "
                    "list operations use status, limit, cursor; create_task uses instruction, context, priority, max_retries; "
                    "update_task uses status and/or priority; execute uses command or code plus language; upload_artifact uses "
                    "filename, mime_type, optional task_id, content_base64; download_artifact uses artifact_id and optional max_bytes."
                ),
            },
        ),
        "workspace",
        risk="write",
        timeout_sec=120.0,
        output_limit=60000,
        implementation=_agent_api,
        uses_caller_context=True,
    ),
    NexusToolDefinition("nexus_apply_patch", "Apply a patch to Nexus when explicitly enabled.", _props(repo={"type": "string"}, patch={"type": "string"}, dry_run={"type": "boolean"}), "write_ops", risk="write", default_enabled=False),
    NexusToolDefinition("nexus_service_restart", "Restart an allowlisted service when explicitly enabled.", _props(service={"type": "string"}, dry_run={"type": "boolean"}), "write_ops", risk="destructive", default_enabled=False),
    NexusToolDefinition("nexus_shell_exec", "Execute an allowlisted shell command when explicitly enabled.", _props(command={"type": "string"}, dry_run={"type": "boolean"}), "shell", risk="shell", default_enabled=False),
    NexusToolDefinition("nexus_python_sandbox", "Run restricted Python calculations when explicitly enabled.", _props(code={"type": "string"}, timeout_seconds={"type": "integer", "minimum": 1, "maximum": 10}, max_output_chars={"type": "integer", "minimum": 1, "maximum": 20000}), "shell", risk="shell", default_enabled=False),
]


def builtin_tool_definitions() -> dict[str, NexusToolDefinition]:
    return {tool.name: tool for tool in _DEFINITIONS}


def enabled_tool_names(toolsets: set[str]) -> set[str]:
    explicit = _csv(S.NEXUS_TOOL_ENABLED)
    disabled = _csv(S.NEXUS_TOOL_DISABLED)
    return {
        tool.name
        for tool in _DEFINITIONS
        if tool.toolset in toolsets and tool.name not in disabled and (tool.default_enabled or tool.name in explicit) and tool.implementation is not None
    }


def openai_tools_for_policy(toolsets: set[str]) -> list[dict[str, Any]]:
    definitions = builtin_tool_definitions()
    return [definitions[name].as_openai() for name in sorted(enabled_tool_names(toolsets))]
