from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import ipaddress
import json
import logging
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
import platform
import sys

try:
    import resource  # type: ignore
except Exception:  # pragma: no cover
    resource = None  # type: ignore

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.auth import require_bearer
from app.agent_api.auth import AgentToolCaller, agent_tool_caller_from_request
from app.agent_api.constants import OPERATIONS as AGENT_API_OPERATIONS
from app.backends import backend_provider_name, llm_backends
from app.config import S
from app.models import ToolExecRequest
from app.openai_utils import new_id, now_unix
from app.resources_snapshot import build_resources_snapshot


router = APIRouter()


log = logging.getLogger(__name__)


from app import metrics
from app import memory_v2
from app import agent_tasks
from app import coding_workspace
from app.upstreams import embed_text_for_memory


_REGISTRY_CACHE: dict[str, Any] = {"path": None, "mtime": None, "tools": {}}


_TOOLS_CONCURRENCY_SEM: threading.Semaphore | None = None


class _ReadableHtmlParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._skip_depth = 0
        self._in_title = False
        self._link_href = ""
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attrs_d = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag_l in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1
            return
        if tag_l == "title":
            self._in_title = True
            return
        if tag_l == "a" and attrs_d.get("href"):
            self._link_href = urljoin(self.base_url, attrs_d.get("href") or "")
            self._link_text = []
        if tag_l in {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if self._skip_depth and tag_l in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth -= 1
            return
        if tag_l == "title":
            self._in_title = False
            return
        if tag_l == "a" and self._link_href:
            text = _collapse_ws(" ".join(self._link_text))
            if len(self.links) < 100:
                self.links.append({"href": self._link_href, "text": text})
            self._link_href = ""
            self._link_text = []
        if tag_l in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = str(data or "")
        if not text.strip():
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._link_href:
            self._link_text.append(text)
        self.text_parts.append(text)
        self.text_parts.append(" ")


def _collapse_ws(value: str) -> str:
    return " ".join(str(value or "").split())


def _run_coroutine_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: Any = None
    error: Exception | None = None

    def runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except Exception as exc:
            error = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    return result


def _embed_text_sync(text: str) -> list[float]:
    return _run_coroutine_sync(embed_text_for_memory(text))


def _tools_concurrency_sem() -> threading.Semaphore:
    global _TOOLS_CONCURRENCY_SEM
    if _TOOLS_CONCURRENCY_SEM is None:
        n = getattr(S, "TOOLS_MAX_CONCURRENT", 8)
        try:
            n = int(n)
        except Exception:
            n = 8
        if n <= 0:
            n = 1
        _TOOLS_CONCURRENCY_SEM = threading.Semaphore(n)
    return _TOOLS_CONCURRENCY_SEM


def _load_tools_registry() -> Dict[str, Dict[str, Any]]:
    """Load explicitly declared tools from an infra-owned JSON file.

    This is *not* automatic discovery; the registry is an explicit declaration list.
    Missing/invalid registry is treated as "no external tools".

    Expected format:
      {"tools": [
        {
          "name": "my_tool",
          "version": "1",
          "description": "...",
          "parameters": { ... JSON Schema ... },
          "exec": {"type": "subprocess", "argv": ["/path/to/bin", "--flag"], "timeout_sec": 10, "cwd": "/tmp"}
        }
      ]}
    """

    path = (getattr(S, "TOOLS_REGISTRY_PATH", "") or "").strip()
    if not path:
        return {}

    try:
        st = os.stat(path)
        mtime = int(st.st_mtime)
    except Exception:
        return {}

    if _REGISTRY_CACHE.get("path") == path and _REGISTRY_CACHE.get("mtime") == mtime:
        tools = _REGISTRY_CACHE.get("tools")
        return tools if isinstance(tools, dict) else {}

    expected_sha = (getattr(S, "TOOLS_REGISTRY_SHA256", "") or "").strip().lower()
    try:
        if expected_sha:
            data = Path(path).read_bytes()
            actual = hashlib.sha256(data).hexdigest().lower()
            if actual != expected_sha:
                try:
                    log.warning("tools registry sha256 mismatch (expected=%s actual=%s); ignoring registry", expected_sha, actual)
                except Exception:
                    pass
                return {}
    except Exception:
        return {}

    tools_out: Dict[str, Dict[str, Any]] = {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        items = payload.get("tools") if isinstance(payload, dict) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                version = item.get("version")
                params = item.get("parameters")
                exec_spec = item.get("exec")
                if not (isinstance(name, str) and name.strip()):
                    continue
                if not (isinstance(version, str) and version.strip()):
                    continue
                if not isinstance(params, dict):
                    continue
                if not (isinstance(exec_spec, dict) and exec_spec.get("type") == "subprocess"):
                    continue
                argv = exec_spec.get("argv")
                if not (isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv)):
                    continue
                tools_out[name.strip()] = {
                    "name": name.strip(),
                    "version": version.strip(),
                    "description": item.get("description") or "",
                    "parameters": params,
                    "exec": {
                        "type": "subprocess",
                        "argv": argv,
                        "timeout_sec": exec_spec.get("timeout_sec"),
                        "cwd": exec_spec.get("cwd"),
                    },
                }
    except Exception:
        tools_out = {}

    _REGISTRY_CACHE["path"] = path
    _REGISTRY_CACHE["mtime"] = mtime
    _REGISTRY_CACHE["tools"] = tools_out
    return tools_out


def _tools_log_path() -> str:
    # Configurable via Settings; default stays within /var/lib/gateway.
    return (S.TOOLS_LOG_PATH or "/var/lib/gateway/data/tools/invocations.jsonl").strip()

def _tools_log_mode() -> str:
    return getattr(S, "TOOLS_LOG_MODE", "ndjson")

def _tools_log_dir() -> str:
    return getattr(S, "TOOLS_LOG_DIR", "/var/lib/gateway/data/tools")

def _write_jsonl_line(path: str, event: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")

def _write_invocation_file(replay_id: str, event: Dict[str, Any]) -> None:
    base_dir = _tools_log_dir()
    os.makedirs(base_dir, exist_ok=True)
    # replay_id is generated internally (req-*/tool-*), safe for filenames.
    path = os.path.join(base_dir, f"{replay_id}.json")
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.write("\n")

def _log_tool_event(replay_id: str, event: Dict[str, Any]) -> None:
    mode = _tools_log_mode()
    if mode in ("ndjson", "both"):
        _write_jsonl_line(_tools_log_path(), event)
    if mode in ("per_invocation", "both"):
        _write_invocation_file(replay_id, event)


_WARNED_UNDECLARED_ALLOWLIST: set[str] = set()


_RATE_LOCK = threading.Lock()
_RATE_STATE: dict[str, dict[str, float]] = {}


def _bearer_token(req: Request) -> str:
    try:
        auth = (req.headers.get("authorization") or "").strip()
    except Exception:
        auth = ""
    if not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()


def _token_policy(req: Request) -> dict:
    try:
        pol = getattr(req.state, "token_policy", None)
        return pol if isinstance(pol, dict) else {}
    except Exception:
        return {}


def _rate_limit(req: Request) -> None:
    """Optional token-bucket rate limit for /v1/tools endpoints."""

    try:
        pol = _token_policy(req)
        rps = float(pol.get("tools_rate_limit_rps", getattr(S, "TOOLS_RATE_LIMIT_RPS", 0.0)) or 0.0)
        burst = int(pol.get("tools_rate_limit_burst", getattr(S, "TOOLS_RATE_LIMIT_BURST", 0)) or 0)
    except Exception:
        rps = 0.0
        burst = 0
    if rps <= 0.0 or burst <= 0:
        return

    tok = _bearer_token(req)
    if not tok:
        return

    now = time.monotonic()
    with _RATE_LOCK:
        st = _RATE_STATE.get(tok)
        if not st:
            st = {"tokens": float(burst), "t": now}
            _RATE_STATE[tok] = st
        tokens = float(st.get("tokens", 0.0))
        last = float(st.get("t", now))

        # refill
        dt = max(0.0, now - last)
        tokens = min(float(burst), tokens + dt * rps)
        if tokens < 1.0:
            st["tokens"] = tokens
            st["t"] = now
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate limited",
                    "error_type": "rate_limited",
                    "error_message": "rate limited",
                },
            )
        tokens -= 1.0
        st["tokens"] = tokens
        st["t"] = now


def _warn_allowlisted_undeclared(name: str) -> None:
    if not isinstance(name, str) or not name:
        return
    if name in _WARNED_UNDECLARED_ALLOWLIST:
        return
    _WARNED_UNDECLARED_ALLOWLIST.add(name)
    try:
        log.warning("tools: allowlisted but undeclared: %s", name)
    except Exception:
        pass


def _resolve_declared_tool(name: str) -> tuple[dict | None, dict | None, str]:
    """Resolve a tool declaration.

    Returns:
      (schema, registry_def, source)

    - schema: tool schema dict (either builtin schema or registry entry)
    - registry_def: registry entry if source == "registry", else None
    - source: "builtin"|"registry"|"missing"
    """

    registry = _load_tools_registry()
    reg_def = registry.get(name) if isinstance(registry, dict) else None
    if isinstance(reg_def, dict):
        return reg_def, reg_def, "registry"
    sch = TOOL_SCHEMAS.get(name)
    if isinstance(sch, dict):
        return sch, None, "builtin"
    return None, None, "missing"


def _tool_category(name: str) -> str:
    if name in {"list_dir", "search_files", "search_text"}:
        return "discovery"
    if name in {"read_file", "read_file_lines"}:
        return "read_files"
    if name in {"web_browse", "http_fetch", "http_fetch_local"}:
        return "network"
    if name in {"coding_task_monitor", "coding_task_inspect"}:
        return "introspection"
    if name in {"current_time", "tool_manifest", "noop", "system_info", "models_refresh", "cluster_resources"}:
        return "introspection"
    if name.startswith("agent_task_"):
        return "scheduling"
    if name.startswith("memory_"):
        return "memory"
    if name in {"coding_task_create", "coding_task_intervene", "nexus_agent_api"}:
        return "workspace"
    if name in {"write_file", "git", "shell", "coding_model_integration"}:
        return "workspace"
    return "specialized"


def tool_manifest_for_names(names: set[str] | list[str] | tuple[str, ...], *, include_parameters: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in sorted({str(n).strip() for n in names if isinstance(n, str) and str(n).strip()}):
        sch, _reg_def, src = _resolve_declared_tool(name)
        if not isinstance(sch, dict):
            continue
        item: dict[str, Any] = {
            "name": str(sch.get("name") or name),
            "version": str(sch.get("version") or ""),
            "category": _tool_category(name),
            "description": str(sch.get("description") or ""),
            "source": src,
        }
        if include_parameters and isinstance(sch.get("parameters"), dict):
            item["parameters"] = sch["parameters"]
        out.append(item)
    return out


def tool_usage_guidance(names: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    allowed = {str(n).strip() for n in names if isinstance(n, str) and str(n).strip()}
    guidance: list[str] = []
    if "tool_manifest" in allowed:
        guidance.append("Use tool_manifest when you need to inspect your currently granted tool capabilities.")
    if allowed.intersection({"list_dir", "search_files", "search_text"}):
        guidance.append("Use discovery tools before broad reading: list_dir for directory shape, search_files for names, search_text for literal code/content matches.")
    if "read_file_lines" in allowed:
        guidance.append("Prefer read_file_lines for targeted file inspection; use read_file only when you need the whole bounded file.")
    if "web_browse" in allowed:
        guidance.append("Use web_browse for current public documentation, issue pages, and other external facts that may have changed.")
    if "current_time" in allowed:
        guidance.append("Use current_time before reasoning about freshness, schedules, or relative dates.")
    if "cluster_resources" in allowed:
        guidance.append("Use cluster_resources when you need current Nexus host resources, backend availability, control-plane health, or the full Resources UI snapshot.")
    if allowed.intersection({"agent_task_create", "agent_task_list", "agent_task_cancel"}):
        guidance.append("Use agent_task_create for reminders, countdowns, recurring checks, and follow-up work; use agent_task_list/cancel to inspect or stop scheduled work.")
    if allowed.intersection({"coding_task_monitor", "coding_task_inspect", "coding_task_intervene"}):
        guidance.append("Use coding_task_monitor to triage coding workspaces, coding_task_inspect for one workspace, and coding_task_intervene only for bounded actions like resume or guidance.")
    if "coding_task_create" in allowed:
        guidance.append("Use coding_task_create to create a general coding workspace only when the scheduled task has a concrete implementation prompt and target repository.")
    if "nexus_agent_api" in allowed:
        guidance.append("Use nexus_agent_api for authenticated coding workspace lifecycle, task, execution, and artifact operations; begin with me or list_workspaces when identifiers are unknown.")
    if "coding_task_notify" in allowed:
        guidance.append("Use coding_task_notify when Nexus Sentinel finds a user-facing noteworthy update that should be sent as a Telegram alert for a coding workspace owner.")
    if allowed.intersection({"write_file", "git", "shell"}):
        guidance.append("Treat write_file, git, and shell as higher-impact tools; inspect first and keep changes scoped.")
    if "coding_model_integration" in allowed:
        guidance.append("Use coding_model_integration to bootstrap a coding workspace for adapting a HuggingFace model into a Nexus backend with scaffolded runtime files.")
    return guidance


def tool_awareness_text(names: set[str] | list[str] | tuple[str, ...], *, include_parameters: bool = False) -> str:
    manifest = tool_manifest_for_names(names, include_parameters=include_parameters)
    if not manifest:
        return "Available Nexus tools: none."
    lines = ["Available Nexus tools:"]
    for item in manifest:
        lines.append(f"- {item['name']} ({item['category']}): {item['description']}")
    guidance = tool_usage_guidance([str(item.get("name") or "") for item in manifest])
    if guidance:
        lines.append("Tool-use guidance:")
        for item in guidance:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _truncate(s: Any, *, max_chars: int) -> Any:
    if isinstance(s, str) and len(s) > max_chars:
        return s[:max_chars] + "…"
    return s


def _safe_json(obj: Any, *, max_chars: int = 20_000) -> str:
    try:
        return _truncate(json.dumps(obj, separators=(",", ":"), sort_keys=True), max_chars=max_chars)  # type: ignore[return-value]
    except Exception:
        return "{}"


def _request_hash(*, tool: str, version: str, args: Dict[str, Any]) -> str:
    """Deterministic request hash for replay/correlation.

    Uses canonical JSON (sorted keys, compact separators).
    """

    payload = {"tool": tool, "version": version, "arguments": args}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _run_subprocess_tool(*, exec_spec: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    argv = exec_spec.get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv)):
        return {"ok": False, "error": "invalid exec spec (argv)"}

    timeout = exec_spec.get("timeout_sec")
    try:
        timeout_sec = float(timeout) if timeout is not None else float(S.TOOLS_SHELL_TIMEOUT_SEC)
    except Exception:
        timeout_sec = float(S.TOOLS_SHELL_TIMEOUT_SEC)

    cwd = exec_spec.get("cwd")
    using_default_cwd = False
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = S.TOOLS_SHELL_CWD
        using_default_cwd = True

    try:
        os.makedirs(cwd, exist_ok=True)
    except Exception as e:
        if using_default_cwd:
            cwd = tempfile.mkdtemp(prefix="gateway-tools-")
        else:
            return {"ok": False, "error": f"cwd not writable: {type(e).__name__}: {e}"}

    stdin_text = json.dumps(args, separators=(",", ":"), sort_keys=True)
    try:
        cp = subprocess.run(
            argv,
            input=stdin_text,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=os.environ,
            check=False,
        )
        try:
            so_max = int(getattr(S, "TOOLS_SUBPROCESS_STDOUT_MAX_CHARS", 20000))
        except Exception:
            so_max = 20000
        try:
            se_max = int(getattr(S, "TOOLS_SUBPROCESS_STDERR_MAX_CHARS", 20000))
        except Exception:
            se_max = 20000
        if so_max <= 0:
            so_max = 20000
        if se_max <= 0:
            se_max = 20000

        stdout = (cp.stdout or "")[-so_max:]
        stderr = (cp.stderr or "")[-se_max:]

        stdout_json = None
        try:
            s = stdout.strip()
            if s:
                stdout_json = json.loads(s)
        except Exception:
            stdout_json = None

        return {
            "ok": cp.returncode == 0,
            "exit_code": int(cp.returncode),
            "stdout": stdout,
            "stdout_json": stdout_json,
            "stderr": stderr,
            "__io_bytes": len(stdin_text.encode("utf-8"))
            + len(stdout.encode("utf-8", errors="ignore"))
            + len(stderr.encode("utf-8", errors="ignore")),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stdout_json": None,
            "stderr": f"timeout after {timeout_sec}s",
            "__io_bytes": len(stdin_text.encode("utf-8")),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "__io_bytes": len(stdin_text.encode("utf-8"))}


def _attach_stdout_json(out: Dict[str, Any]) -> None:
    """If tool returns stdout, expose a parsed stdout_json field.

    This helps clients/tools return structured output deterministically while
    preserving the raw stdout string.
    """

    if "stdout_json" in out:
        return
    stdout = out.get("stdout")
    if not isinstance(stdout, str):
        return
    parsed = None
    try:
        s = stdout.strip()
        if s:
            parsed = json.loads(s)
    except Exception:
        parsed = None
    out["stdout_json"] = parsed


def _normalize_tool_error(out: Dict[str, Any]) -> None:
    """Ensure a consistent error envelope for tool failures.

    Adds:
      - error_type
      - error_message

    Preserves the existing 'error' field for backward compatibility.
    """

    try:
        if bool(out.get("ok")) is True:
            return

        et = out.get("error_type")
        em = out.get("error_message")
        if isinstance(et, str) and et and isinstance(em, str) and em:
            return

        err = out.get("error")
        if isinstance(err, str) and err:
            # We often format errors as "TypeName: message".
            if ": " in err:
                head, tail = err.split(": ", 1)
                out.setdefault("error_type", head.strip() or "tool_error")
                out.setdefault("error_message", tail.strip() or err)
            else:
                out.setdefault("error_type", "tool_error")
                out.setdefault("error_message", err)
            return

        exit_code = out.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            out.setdefault("error_type", "subprocess_nonzero_exit")
            out.setdefault("error_message", f"exit_code={exit_code}")
            return

        stderr = out.get("stderr")
        if isinstance(stderr, str) and stderr.strip():
            out.setdefault("error_type", "stderr")
            out.setdefault("error_message", stderr.strip())
            return

        out.setdefault("error_type", "tool_error")
        out.setdefault("error_message", "tool failed")
    except Exception:
        return


def _normalize_tool_result(out: Any) -> Dict[str, Any]:
    """Normalize a tool implementation result into a dict with boolean ok.

    Tool implementations are expected to return a dict with an 'ok' boolean.
    Anything else is treated as an invalid tool result.
    """

    if not isinstance(out, dict):
        return {
            "ok": False,
            "error": "invalid tool result",
            "error_type": "invalid_tool_result",
            "error_message": "tool returned a non-object result",
        }
    ok = out.get("ok")
    if not isinstance(ok, bool):
        # Preserve the raw output for debugging, but keep it bounded.
        return {
            "ok": False,
            "error": "invalid tool result",
            "error_type": "invalid_tool_result",
            "error_message": "tool result missing boolean 'ok'",
            "result": _truncate(out, max_chars=10_000),
        }
    return out


def _validate_against_schema(params_schema: Dict[str, Any], args: Any) -> list[str]:
    """Minimal validation for our tool parameter schemas.

    Supports:
    - object schemas with properties/required/additionalProperties
    - string
    - array of strings
    """

    errs: list[str] = []
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]

    if (params_schema.get("type") or "") != "object":
        return []

    props = params_schema.get("properties")
    if not isinstance(props, dict):
        props = {}

    required = params_schema.get("required")
    if isinstance(required, list):
        for k in required:
            if isinstance(k, str) and k not in args:
                errs.append(f"missing required field: {k}")

    additional = params_schema.get("additionalProperties")
    if additional is False:
        allowed = set(k for k in props.keys() if isinstance(k, str))
        extra = sorted([k for k in args.keys() if k not in allowed])
        for k in extra:
            errs.append(f"unexpected field: {k}")

    for key, sch in props.items():
        if not isinstance(key, str) or key not in args:
            continue
        v = args.get(key)
        if not isinstance(sch, dict):
            continue
        t = sch.get("type")
        if t == "string":
            if not isinstance(v, str):
                errs.append(f"{key} must be a string")
        elif t == "array":
            items = sch.get("items")
            if not isinstance(v, list):
                errs.append(f"{key} must be an array")
            else:
                if isinstance(items, dict) and items.get("type") == "string":
                    if not all(isinstance(x, str) for x in v):
                        errs.append(f"{key} items must be strings")
        elif t == "object":
            if not isinstance(v, dict):
                errs.append(f"{key} must be an object")

    return errs


def tool_shell(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_SHELL:
        return {"ok": False, "error": "shell tool disabled"}

    cmd = args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return {"ok": False, "error": "cmd must be a non-empty string"}

    cwd = S.TOOLS_SHELL_CWD
    try:
        os.makedirs(cwd, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": f"cwd not writable: {type(e).__name__}: {e}"}

    allowed = {p.strip() for p in (S.TOOLS_SHELL_ALLOWED_CMDS or "").split(",") if p.strip()}
    if not allowed:
        return {"ok": False, "error": "shell tool not configured (TOOLS_SHELL_ALLOWED_CMDS empty)"}

    try:
        parts = shlex.split(cmd)
        if not parts:
            return {"ok": False, "error": "cmd must be a non-empty string"}
        exe = parts[0]
        if exe not in allowed:
            return {"ok": False, "error": f"command not allowed: {exe}"}
        cp = subprocess.run(
            parts,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=S.TOOLS_SHELL_TIMEOUT_SEC,
            check=False,
        )
        return {
            "ok": True,
            "returncode": cp.returncode,
            "stdout": cp.stdout[-20000:],
            "stderr": cp.stderr[-20000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {S.TOOLS_SHELL_TIMEOUT_SEC}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _tools_fs_roots() -> list[Path]:
    roots = [r.strip() for r in (S.TOOLS_FS_ROOTS or "").split(",") if r.strip()]
    out: list[Path] = []
    for root in roots:
        try:
            out.append(Path(root).resolve())
        except Exception:
            continue
    return out


def _resolve_tools_path(path: str, *, roots: list[Path] | None = None) -> Path:
    roots = roots if roots is not None else _tools_fs_roots()
    if not roots:
        raise ValueError("fs tool not configured (TOOLS_FS_ROOTS empty)")
    p = Path(path)
    if not p.is_absolute():
        p = roots[0] / p
    p = p.resolve()
    for root in roots:
        try:
            p.relative_to(root)
            return p
        except Exception:
            continue
    raise ValueError("path outside allowed roots")


def _max_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value) if value is not None else default
    except Exception:
        n = default
    return max(minimum, min(n, maximum))


def _path_is_probably_text(path: Path, *, max_probe_bytes: int = 4096) -> bool:
    try:
        with open(path, "rb") as f:
            data = f.read(max_probe_bytes)
        if b"\x00" in data:
            return False
        return True
    except Exception:
        return False


def tool_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}

    try:
        p = _resolve_tools_path(path)

        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        with open(p, "rb") as f:
            data = f.read(max_bytes + 1)

        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        return {"ok": True, "path": str(p), "truncated": truncated, "content": text, "__io_bytes": len(data)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_read_file_lines(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    start = _max_int(args.get("start"), default=1, minimum=1, maximum=10_000_000)
    limit = _max_int(args.get("limit"), default=120, minimum=1, maximum=1_000)
    try:
        p = _resolve_tools_path(path)
        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        lines: list[dict[str, Any]] = []
        bytes_read = 0
        truncated = False
        with open(p, "rb") as f:
            for idx, raw in enumerate(f, start=1):
                bytes_read += len(raw)
                if bytes_read > max_bytes:
                    truncated = True
                    break
                if idx < start:
                    continue
                if len(lines) >= limit:
                    truncated = True
                    break
                lines.append({"line": idx, "text": raw.decode("utf-8", errors="replace").rstrip("\r\n")})
        return {"ok": True, "path": str(p), "start": start, "limit": limit, "lines": lines, "truncated": truncated, "__io_bytes": min(bytes_read, max_bytes)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_list_dir(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path") or "."
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    limit = _max_int(args.get("limit"), default=200, minimum=1, maximum=2_000)
    include_hidden = bool(args.get("include_hidden", False))
    try:
        p = _resolve_tools_path(path)
        if not p.is_dir():
            return {"ok": False, "error": "path is not a directory", "path": str(p)}
        entries: list[dict[str, Any]] = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if not include_hidden and child.name.startswith("."):
                continue
            try:
                st = child.stat()
                kind = "dir" if child.is_dir() else "file" if child.is_file() else "other"
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "type": kind,
                        "size": int(st.st_size),
                        "mtime": int(st.st_mtime),
                    }
                )
            except Exception:
                entries.append({"name": child.name, "path": str(child), "type": "unknown"})
            if len(entries) >= limit:
                break
        return {"ok": True, "path": str(p), "entries": entries, "truncated": len(entries) >= limit}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_search_files(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path") or "."
    pattern = args.get("pattern") or "*"
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    if not isinstance(pattern, str) or not pattern:
        return {"ok": False, "error": "pattern must be a non-empty string"}
    limit = _max_int(args.get("limit"), default=200, minimum=1, maximum=2_000)
    max_entries = _max_int(args.get("max_entries"), default=10_000, minimum=1, maximum=100_000)
    include_hidden = bool(args.get("include_hidden", False))
    include_dirs = bool(args.get("include_dirs", True))
    try:
        roots = _tools_fs_roots()
        base = _resolve_tools_path(path, roots=roots)
        if not base.exists():
            return {"ok": False, "error": "path does not exist", "path": str(base)}
        matches: list[dict[str, Any]] = []
        inspected = 0
        stack = [base]
        while stack and len(matches) < limit and inspected < max_entries:
            current = stack.pop()
            inspected += 1
            if current.is_dir():
                try:
                    children = sorted(current.iterdir(), key=lambda c: c.name.lower(), reverse=True)
                except Exception:
                    continue
                for child in children:
                    if inspected >= max_entries:
                        break
                    inspected += 1
                    if not include_hidden and any(part.startswith(".") for part in child.relative_to(base).parts):
                        continue
                    if child.is_dir():
                        stack.append(child)
                    name_matches = fnmatch.fnmatch(child.name, pattern) or fnmatch.fnmatch(str(child.relative_to(base)), pattern)
                    if name_matches and (include_dirs or not child.is_dir()):
                        try:
                            st = child.stat()
                            matches.append({"path": str(child), "relative_path": str(child.relative_to(base)), "type": "dir" if child.is_dir() else "file", "size": int(st.st_size)})
                        except Exception:
                            matches.append({"path": str(child), "relative_path": str(child.relative_to(base)), "type": "unknown"})
                        if len(matches) >= limit:
                            break
            else:
                if fnmatch.fnmatch(current.name, pattern):
                    matches.append({"path": str(current), "relative_path": current.name, "type": "file", "size": int(current.stat().st_size)})
        return {"ok": True, "path": str(base), "pattern": pattern, "matches": matches, "inspected": inspected, "truncated": len(matches) >= limit or bool(stack) or inspected >= max_entries}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_search_text(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path") or "."
    query = args.get("query")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    if not isinstance(query, str) or not query:
        return {"ok": False, "error": "query must be a non-empty string"}
    pattern = args.get("file_pattern") or "*"
    if not isinstance(pattern, str) or not pattern:
        return {"ok": False, "error": "file_pattern must be a non-empty string"}
    case_sensitive = bool(args.get("case_sensitive", False))
    limit = _max_int(args.get("limit"), default=100, minimum=1, maximum=1_000)
    context_lines = _max_int(args.get("context_lines"), default=0, minimum=0, maximum=5)
    max_files = _max_int(args.get("max_files"), default=3_000, minimum=1, maximum=20_000)
    max_total_bytes = _max_int(args.get("max_total_bytes"), default=5_000_000, minimum=10_000, maximum=50_000_000)
    include_hidden = bool(args.get("include_hidden", False))
    try:
        roots = _tools_fs_roots()
        base = _resolve_tools_path(path, roots=roots)
        files = [base] if base.is_file() else []
        if base.is_dir():
            for candidate in base.rglob("*"):
                if len(files) >= max_files:
                    break
                try:
                    rel = candidate.relative_to(base)
                except Exception:
                    continue
                if not include_hidden and any(part.startswith(".") for part in rel.parts):
                    continue
                if candidate.is_file() and (fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(str(rel), pattern)):
                    files.append(candidate)
        needle = query if case_sensitive else query.lower()
        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        matches: list[dict[str, Any]] = []
        io_bytes = 0
        for file_path in files:
            if len(matches) >= limit or io_bytes >= max_total_bytes:
                break
            if not _path_is_probably_text(file_path):
                continue
            try:
                remaining = max_total_bytes - io_bytes
                raw = file_path.read_bytes()[: min(max_bytes, remaining) + 1]
            except Exception:
                continue
            allowed_bytes = min(len(raw), max_bytes, max(0, max_total_bytes - io_bytes))
            io_bytes += allowed_bytes
            text = raw[:allowed_bytes].decode("utf-8", errors="replace")
            lines = text.splitlines()
            for idx, line in enumerate(lines, start=1):
                hay = line if case_sensitive else line.lower()
                if needle not in hay:
                    continue
                start_idx = max(1, idx - context_lines)
                end_idx = min(len(lines), idx + context_lines)
                item: dict[str, Any] = {
                    "path": str(file_path),
                    "relative_path": str(file_path.relative_to(base)) if base.is_dir() else file_path.name,
                    "line": idx,
                    "text": line[:2_000],
                }
                if context_lines:
                    item["context"] = [{"line": n, "text": lines[n - 1][:2_000]} for n in range(start_idx, end_idx + 1)]
                matches.append(item)
                if len(matches) >= limit:
                    break
        return {
            "ok": True,
            "path": str(base),
            "query": query,
            "matches": matches,
            "files_considered": len(files),
            "truncated": len(matches) >= limit or len(files) >= max_files or io_bytes >= max_total_bytes,
            "__io_bytes": io_bytes,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    if not S.TOOLS_ALLOW_FS_WRITE:
        return {"ok": False, "error": "fs write disabled"}
    path = args.get("path")
    content = args.get("content", "")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    roots = [r.strip() for r in (S.TOOLS_FS_ROOTS or "").split(",") if r.strip()]
    if not roots:
        return {"ok": False, "error": "fs tool not configured (TOOLS_FS_ROOTS empty)"}

    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(roots[0]) / p
        p = p.resolve()

        allowed_root = False
        for r in roots:
            try:
                root_path = Path(r).resolve()
                p.relative_to(root_path)
                allowed_root = True
                break
            except Exception:
                continue
        if not allowed_root:
            return {"ok": False, "error": "path outside allowed roots"}

        # Basic size limit to avoid large writes.
        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > max_bytes:
            return {"ok": False, "error": f"content too large (>{max_bytes} bytes)"}

        os.makedirs(str(p.parent), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": str(p), "__io_bytes": len(content_bytes)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_http_fetch(args: Dict[str, Any], *, override_allowed_hosts: set[str] | None = None) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_HTTP_FETCH:
        return {"ok": False, "error": "http_fetch tool disabled"}

    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url must be a non-empty string"}

    method = (args.get("method") or "GET").strip().upper()
    if method != "GET":
        return {"ok": False, "error": "only GET is supported"}

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"ok": False, "error": "only http/https URLs are allowed"}

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return {"ok": False, "error": "url must include a hostname"}

    allowed_hosts = (
        {h.strip().lower() for h in (S.TOOLS_HTTP_ALLOWED_HOSTS or "").split(",") if h.strip()}
        if override_allowed_hosts is None
        else override_allowed_hosts
    )
    if host not in allowed_hosts:
        return {"ok": False, "error": f"host not allowed: {host}"}

    hdrs = args.get("headers")
    if hdrs is None:
        headers = {}
    elif isinstance(hdrs, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in hdrs.items()):
        headers = hdrs
    else:
        return {"ok": False, "error": "headers must be an object of string:string"}

    max_bytes = int(S.TOOLS_HTTP_MAX_BYTES)
    timeout = float(S.TOOLS_HTTP_TIMEOUT_SEC)

    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("GET", url, headers=headers) as r:
                status = r.status_code
                out = bytearray()
                for chunk in r.iter_bytes():
                    if not chunk:
                        continue
                    remaining = max_bytes - len(out)
                    if remaining <= 0:
                        break
                    out.extend(chunk[:remaining])
                content_type = r.headers.get("content-type", "")

        body_text = None
        try:
            body_text = out.decode("utf-8")
        except Exception:
            body_text = None

        return {
            "ok": True,
            "status": status,
            "content_type": content_type,
            "truncated": len(out) >= max_bytes,
            "body_text": body_text,
            "body_base64": None if body_text is not None else base64.b64encode(bytes(out)).decode("ascii"),
            "__io_bytes": len(out),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_http_fetch_local(args: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch a URL via GET, hard-restricted to localhost.

    This is a safer variant for internal self-checks (e.g. fetching /health).
    """

    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url must be a non-empty string"}

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"ok": False, "error": "only http/https URLs are allowed"}

    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return {"ok": False, "error": f"host not allowed: {host}"}

    # Delegate to the main implementation (which enforces GET + size limits).
    return tool_http_fetch(args, override_allowed_hosts={"127.0.0.1", "localhost", "::1"})


def _web_browse_max_bytes(value: Any = None) -> int:
    default = int(getattr(S, "TOOLS_WEB_BROWSE_MAX_BYTES", 1_000_000) or 1_000_000)
    try:
        requested = int(value) if value is not None else default
    except Exception:
        requested = default
    return max(1_000, min(requested, default, 5_000_000))


def _web_browse_timeout_sec(value: Any = None) -> float:
    default = float(getattr(S, "TOOLS_WEB_BROWSE_TIMEOUT_SEC", 20.0) or 20.0)
    try:
        requested = float(value) if value is not None else default
    except Exception:
        requested = default
    return max(1.0, min(requested, default, 60.0))


def _web_browse_allowed_patterns() -> list[str]:
    raw = str(getattr(S, "TOOLS_WEB_BROWSE_ALLOWED_HOSTS", "*") or "*")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _host_allowed_for_web_browse(host: str) -> bool:
    host_l = str(host or "").strip().lower().rstrip(".")
    if not host_l:
        return False
    for pattern in _web_browse_allowed_patterns():
        if pattern == "*":
            return True
        if pattern.startswith("*.") and host_l.endswith(pattern[1:]):
            return True
        if pattern == host_l:
            return True
    return False


def _host_is_public(host: str) -> bool:
    host_l = str(host or "").strip().lower().rstrip(".")
    if host_l in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(host_l.strip("[]"))
        return bool(ip.is_global)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host_l, None, type=socket.SOCK_STREAM)
    except Exception:
        return False
    addresses = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            addresses.add(str(sockaddr[0]))
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _validate_web_browse_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("url is required")
    if len(raw) > 4096:
        raise ValueError("url is too long")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http and https URLs are supported")
    if not parts.hostname:
        raise ValueError("url host is required")
    if parts.username or parts.password:
        raise ValueError("url credentials are not allowed")
    host = parts.hostname.lower().rstrip(".")
    if not _host_allowed_for_web_browse(host):
        raise ValueError("url host is not allowed")
    if not bool(getattr(S, "TOOLS_WEB_BROWSE_ALLOW_PRIVATE", False)) and not _host_is_public(host):
        raise ValueError("localhost and private-network targets are blocked")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def _decode_response_body(raw: bytes, content_type: str) -> tuple[str, str]:
    charset = ""
    for part in str(content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"')
            break
    for encoding in [charset, "utf-8", "latin-1"]:
        if not encoding:
            continue
        try:
            return raw.decode(encoding, errors="replace"), encoding
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _readable_html(text: str, *, base_url: str) -> dict[str, Any]:
    parser = _ReadableHtmlParser(base_url=base_url)
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    body = "\n".join(_collapse_ws(part) for part in "".join(parser.text_parts).splitlines())
    body = "\n".join(line for line in body.splitlines() if line.strip())
    return {
        "title": _collapse_ws(" ".join(parser.title_parts)),
        "text": body,
        "links": parser.links[:100],
    }


def tool_web_browse(args: Dict[str, Any]) -> Dict[str, Any]:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url must be a non-empty string"}
    max_bytes = _web_browse_max_bytes(args.get("max_bytes"))
    timeout = _web_browse_timeout_sec(args.get("timeout_sec"))
    max_redirects = max(0, min(int(args.get("max_redirects") if args.get("max_redirects") is not None else getattr(S, "TOOLS_WEB_BROWSE_MAX_REDIRECTS", 4)), 8))
    include_html = bool(args.get("include_html"))
    extract_links = bool(args.get("extract_links", True))
    user_agent = str(args.get("user_agent") or "Nexus-WebBrowse/1.0").strip()[:200] or "Nexus-WebBrowse/1.0"
    try:
        current = _validate_web_browse_url(url)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    redirects: list[str] = []
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": user_agent}) as client:
            for _ in range(max_redirects + 1):
                with client.stream("GET", current) as resp:
                    status = int(resp.status_code)
                    location = resp.headers.get("location") or ""
                    if status in {301, 302, 303, 307, 308} and location and len(redirects) < max_redirects:
                        next_url = _validate_web_browse_url(urljoin(current, location))
                        redirects.append(next_url)
                        current = next_url
                        continue
                    out = bytearray()
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        remaining = max_bytes - len(out)
                        if remaining <= 0:
                            break
                        out.extend(chunk[:remaining])
                    content_type = resp.headers.get("content-type", "")
                    final_url = str(resp.url)
                    break
            else:
                return {"ok": False, "error": "too many redirects", "redirects": redirects}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": current, "redirects": redirects}

    raw = bytes(out)
    text, encoding = _decode_response_body(raw, content_type)
    lowered_type = str(content_type or "").lower()
    readable = {"title": "", "text": text, "links": []}
    if "html" in lowered_type or "<html" in text[:1000].lower():
        readable = _readable_html(text, base_url=final_url)
    if not extract_links:
        readable["links"] = []
    result: Dict[str, Any] = {
        "ok": True,
        "url": str(url),
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "encoding": encoding,
        "title": readable.get("title") or "",
        "text": str(readable.get("text") or "")[:max_bytes],
        "links": readable.get("links") or [],
        "redirects": redirects,
        "truncated": len(raw) >= max_bytes,
        "__io_bytes": len(raw),
    }
    if include_html:
        result["html"] = text[:max_bytes]
    return result


def tool_current_time(args: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "unix": int(now.timestamp()),
        "iso_utc": now.isoformat().replace("+00:00", "Z"),
        "timezone": "UTC",
    }


def tool_tool_manifest(args: Dict[str, Any]) -> Dict[str, Any]:
    include_parameters = bool(args.get("include_parameters", False))
    category = args.get("category")
    if category is not None and not isinstance(category, str):
        return {"ok": False, "error": "category must be a string"}
    allowed_raw = args.get("__allowed_tools")
    if isinstance(allowed_raw, (set, list, tuple)):
        names = {str(n).strip() for n in allowed_raw if isinstance(n, str) and str(n).strip()}
    else:
        names = allowed_tool_names_for_policy(None)
    manifest = tool_manifest_for_names(names, include_parameters=include_parameters)
    if isinstance(category, str) and category.strip():
        wanted = category.strip()
        manifest = [item for item in manifest if item.get("category") == wanted]
    return {
        "ok": True,
        "tools": manifest,
        "guidance": tool_usage_guidance([str(item.get("name") or "") for item in manifest]),
    }


def tool_system_info(args: Dict[str, Any]) -> Dict[str, Any]:
    if not getattr(S, "TOOLS_ALLOW_SYSTEM_INFO", False):
        return {"ok": False, "error": "system_info tool disabled"}
    return {
        "ok": True,
        "python": sys.version.split("\n", 1)[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "features": {
            "tools_allow_shell": bool(S.TOOLS_ALLOW_SHELL),
            "tools_allow_fs": bool(S.TOOLS_ALLOW_FS),
            "tools_allow_http_fetch": bool(S.TOOLS_ALLOW_HTTP_FETCH),
            "tools_allow_git": bool(S.TOOLS_ALLOW_GIT),
        },
    }


def tool_cluster_resources(args: Dict[str, Any]) -> Dict[str, Any]:
    if not getattr(S, "TOOLS_ALLOW_CLUSTER_RESOURCES", True):
        return {"ok": False, "error": "cluster_resources tool disabled"}
    refresh = bool(args.get("refresh", False))
    include_sources = bool(args.get("include_sources", False))
    snapshot = _run_coroutine_sync(build_resources_snapshot(refresh_lifecycle=refresh))
    if not isinstance(snapshot, dict):
        return {"ok": False, "error": "invalid cluster resources snapshot"}
    resources = snapshot.get("resources") if isinstance(snapshot.get("resources"), dict) else {}
    hosts = resources.get("hosts") if isinstance(resources.get("hosts"), list) else []
    backends = resources.get("backends") if isinstance(resources.get("backends"), list) else []
    control_plane = resources.get("control_plane") if isinstance(resources.get("control_plane"), list) else []
    core_services = resources.get("core_services") if isinstance(resources.get("core_services"), list) else []
    result: Dict[str, Any] = {
        "ok": bool(snapshot.get("ok")) or bool(resources),
        "resources": resources,
        "summary": {
            "hosts": len(hosts),
            "backends": len(backends),
            "control_plane": len(control_plane),
            "core_services": len(core_services),
            "generated_at": resources.get("generated_at"),
            "mode": resources.get("mode"),
        },
    }
    lifecycle_error = str(snapshot.get("lifecycle_error") or "").strip()
    registry_error = str(snapshot.get("registry_error") or "").strip()
    if lifecycle_error:
        result["lifecycle_error"] = lifecycle_error
    if registry_error:
        result["registry_error"] = registry_error
    if include_sources and isinstance(snapshot.get("sources"), dict):
        result["sources"] = snapshot["sources"]
    if not result["ok"]:
        result["error"] = lifecycle_error or registry_error or "resources unavailable"
    return result


def tool_models_refresh(args: Dict[str, Any]) -> Dict[str, Any]:
    if not getattr(S, "TOOLS_ALLOW_MODELS_REFRESH", False):
        return {"ok": False, "error": "models_refresh tool disabled"}

    out: Dict[str, Any] = {"ok": True, "upstreams": {}}
    timeout = float(getattr(S, "TOOLS_HTTP_TIMEOUT_SEC", 10))
    try:
        with httpx.Client(timeout=timeout) as client:
            for backend_name, cfg in llm_backends():
                provider = backend_provider_name(backend_name)
                try:
                    url = f"{cfg.base_url.rstrip('/')}/models"
                    r = client.get(url)
                    out["upstreams"][backend_name] = {
                        "ok": r.status_code == 200,
                        "status": r.status_code,
                        "provider": provider,
                    }
                except Exception as e:
                    out["ok"] = False
                    out["upstreams"][backend_name] = {
                        "ok": False,
                        "provider": provider,
                        "error": f"{type(e).__name__}: {e}",
                    }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


def tool_coding_model_integration(args: Dict[str, Any]) -> Dict[str, Any]:
    task = coding_workspace.create_model_integration_task(
        model=str(args.get("model") or "").strip(),
        repo_url=str(args.get("repo_url") or "").strip() or None,
        preferred_runtime=str(args.get("preferred_runtime") or "").strip() or None,
        route_kind=str(args.get("route_kind") or "").strip() or None,
        service_name=str(args.get("service_name") or "").strip() or None,
        base_branch=str(args.get("base_branch") or "").strip() or None,
        branch_name=str(args.get("branch_name") or "").strip() or None,
        prompt=str(args.get("prompt") or "").strip() or None,
        owner="tool",
        owner_user_id=None,
        git_token_value=str(args.get("git_token") or "").strip() or None,
        coding_model=str(args.get("coding_model") or "coder").strip() or "coder",
    )
    if bool(args.get("auto_run")) and task.get("status") != "error":
        from app import coding_agent

        task = _run_coroutine_sync(
            coding_agent.start_agent_run(
                str(task.get("id") or ""),
                coding_model=str(args.get("coding_model") or task.get("coding_model") or "coder").strip() or "coder",
                auto_commit=bool(args.get("auto_commit")),
                commit_message=str(args.get("commit_message") or "").strip() or None,
                actor="tool",
            )
        )
    return {"ok": task.get("status") != "error", "task": task}


def tool_coding_task_create(args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}

    git_token_value = str(args.get("git_token") or "").strip() or None
    coding_model = str(args.get("coding_model") or "coder").strip() or "coder"
    mission_overrides = coding_workspace.coding_mission_overrides(
        commit_policy="always_on_success",
        push_on_success=bool(args.get("push_on_success")),
        draft_pr_on_success=bool(args.get("draft_pr_on_success")),
        pr_title=str(args.get("pr_title") or ""),
        pr_body=str(args.get("pr_body") or ""),
        max_cycles=int(args.get("max_cycles") or 1000),
        max_runtime_sec=int(args.get("max_runtime_sec") or 21600),
        context_reset_cycles=int(args.get("context_reset_cycles") or 0),
    )
    if bool(args.get("auto_run")):
        from app import coding_agent

        task = _run_coroutine_sync(
            coding_agent.create_and_start_agent_run(
                repo_url=str(args.get("repo_url") or "").strip() or None,
                base_branch=str(args.get("base_branch") or "").strip() or None,
                branch_name=str(args.get("branch_name") or "").strip() or None,
                prompt=prompt,
                owner="scheduled-task-tool",
                owner_user_id=None,
                git_token_value=git_token_value,
                coding_model=coding_model,
                commit_message=str(args.get("commit_message") or "").strip() or None,
                actor="scheduled-task-tool",
                max_cycles=int(args.get("max_cycles") or 1000),
                max_runtime_sec=int(args.get("max_runtime_sec") or 21600),
                context_reset_cycles=int(args.get("context_reset_cycles") or 0),
                mission_overrides=mission_overrides,
            )
        )
    else:
        task = coding_workspace.create_task(
            repo_url=str(args.get("repo_url") or "").strip() or None,
            base_branch=str(args.get("base_branch") or "").strip() or None,
            branch_name=str(args.get("branch_name") or "").strip() or None,
            prompt=prompt,
            owner="scheduled-task-tool",
            owner_user_id=None,
            git_token_value=git_token_value,
            coding_model=coding_model,
            mission_overrides=mission_overrides,
        )
    return {"ok": task.get("status") != "error", "task": task}


def tool_nexus_agent_api(args: Dict[str, Any]) -> Dict[str, Any]:
    from app.agent_api.tool import execute_agent_api_tool

    caller = args.pop("__caller", None)
    return _run_coroutine_sync(
        execute_agent_api_tool(
            args,
            caller if isinstance(caller, AgentToolCaller) else None,
        )
    )


def tool_coding_task_monitor(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 20)
    stalled_after_sec = float(args.get("stalled_after_sec") or 900.0)
    only_attention = bool(args.get("only_attention"))
    return coding_workspace.monitor_tasks(limit=limit, only_attention=only_attention, stalled_after_sec=stalled_after_sec)


def tool_coding_task_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("task_id") or args.get("id") or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    stalled_after_sec = float(args.get("stalled_after_sec") or 900.0)
    return coding_workspace.inspect_task(task_id, stalled_after_sec=stalled_after_sec)


def tool_coding_task_intervene(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("task_id") or args.get("id") or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    action = str(args.get("action") or "").strip().lower()
    if action not in {"resume", "guidance", "guide_and_resume", "pause", "stop"}:
        return {"ok": False, "error": "action must be one of resume, guidance, guide_and_resume, pause"}
    message = str(args.get("message") or "").strip()
    actor = str(args.get("actor") or "coding-supervisor").strip() or "coding-supervisor"

    try:
        task = coding_workspace.load_task(task_id)
    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}
    public = coding_workspace.public_task(task, include_commands=False)
    agent = public.get("agent") if isinstance(public.get("agent"), dict) else {}
    active = str(agent.get("status") or "") in {"queued", "running", "stopping", "pausing"}

    if action == "guidance":
        if not message:
            return {"ok": False, "error": "message is required for guidance"}
        updated = coding_workspace.append_guidance_message(task_id, message=message, actor=actor)
        return {"ok": True, "action": action, "started": False, "task": updated}

    if action == "resume":
        if active:
            return {"ok": False, "error": "coding task is already running", "task": public}
        from app import coding_agent

        updated = _run_coroutine_sync(
            coding_agent.start_agent_run(
                task_id,
                prompt=message or None,
                actor=actor,
            )
        )
        return {"ok": True, "action": action, "started": True, "task": updated}

    if action == "guide_and_resume":
        if not message:
            return {"ok": False, "error": "message is required for guide_and_resume"}
        coding_workspace.append_guidance_message(task_id, message=message, actor=actor)
        if active:
            updated = coding_workspace.public_task(coding_workspace.load_task(task_id), include_commands=False)
            return {"ok": True, "action": action, "started": False, "task": updated}
        from app import coding_agent

        updated = _run_coroutine_sync(
            coding_agent.start_agent_run(
                task_id,
                prompt=message,
                actor=actor,
            )
        )
        return {"ok": True, "action": action, "started": True, "task": updated}

    from app import coding_agent

    updated = _run_coroutine_sync(coding_agent.request_pause(task_id))
    action = "pause" if action == "stop" else action
    return {"ok": True, "action": action, "started": False, "task": updated}


def tool_coding_task_notify(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("task_id") or args.get("id") or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    summary = str(args.get("summary") or args.get("message") or "").strip()
    if not summary:
        return {"ok": False, "error": "summary is required"}
    severity = str(args.get("severity") or "info").strip().lower() or "info"
    dedupe_key = str(args.get("dedupe_key") or "").strip() or hashlib.sha1(f"{severity}:{summary}".encode("utf-8")).hexdigest()
    try:
        cooldown_sec = max(0, int(args.get("cooldown_sec") or 6 * 60 * 60))
    except Exception:
        cooldown_sec = 6 * 60 * 60
    actor = str(args.get("actor") or "Nexus Sentinel").strip() or "Nexus Sentinel"

    from app import telegram_notifications

    try:
        task = coding_workspace.load_task(task_id)
    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}

    public = coding_workspace.public_task(task, include_commands=False)
    target = telegram_notifications.resolve_notification_target(
        user_id=task.get("owner_user_id"),
        owner_username=public.get("owner"),
        app="coding",
    )
    if not bool(target.get("enabled")):
        return {"ok": True, "sent": False, "reason": str(target.get("reason") or "disabled")}
    if not bool(target.get("notify_on_noteworthy")):
        return {"ok": True, "sent": False, "reason": "noteworthy_disabled"}

    now_ts = time.time()
    events = task.get("agent_events")
    if not isinstance(events, list):
        events = []
    previous_ts = 0.0
    for item in reversed(events):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "telegram_notification":
            continue
        if str(item.get("category") or "") != "noteworthy":
            continue
        if str(item.get("dedupe_key") or "") != dedupe_key:
            continue
        try:
            previous_ts = float(item.get("ts") or 0)
        except Exception:
            previous_ts = 0.0
        break
    if cooldown_sec > 0 and previous_ts > 0 and (now_ts - previous_ts) < cooldown_sec:
        return {
            "ok": True,
            "sent": False,
            "reason": "cooldown",
            "retry_after_sec": max(0, cooldown_sec - int(now_ts - previous_ts)),
        }

    text = telegram_notifications.render_coding_workspace_notification(
        item={
            "id": public.get("id"),
            "owner": public.get("owner"),
            "status": public.get("status"),
            "agent": public.get("agent") if isinstance(public.get("agent"), dict) else {},
            "recommended_action": "",
            "attention": [],
        },
        event_kind="noteworthy",
        mention_username=str(target.get("mention_username") or ""),
        note=summary,
        severity=severity,
    )
    result = _run_coroutine_sync(
        telegram_notifications.send_message(
            chat_id=str(target.get("chat_id") or ""),
            text=text,
        )
    )
    sent = bool(isinstance(result, dict) and result.get("ok"))
    if sent:
        events.append(
            {
                "ts": now_ts,
                "type": "telegram_notification",
                "category": "noteworthy",
                "dedupe_key": dedupe_key,
                "severity": severity,
                "summary": summary,
                "actor": actor,
            }
        )
        task["agent_events"] = events[-max(20, min(int(getattr(S, "CODING_AGENT_MAX_EVENTS", 1000) or 1000), 1000)) :]
        task["agent_last_event_at"] = now_ts
        coding_workspace.save_task(task)
    return {
        "ok": sent,
        "sent": sent,
        "chat_id": str(target.get("chat_id") or ""),
        "severity": severity,
        "dedupe_key": dedupe_key,
        "reason": str(result.get("error") or "") if isinstance(result, dict) and not sent else "",
    }


def tool_git(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_GIT:
        return {"ok": False, "error": "git tool disabled"}

    argv = args.get("args")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        return {"ok": False, "error": "args must be a non-empty list of strings"}

    subcmd = argv[0].strip()
    allowed_subcmds = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
    if subcmd not in allowed_subcmds:
        return {"ok": False, "error": f"git subcommand not allowed: {subcmd}"}

    cwd = (S.TOOLS_GIT_CWD or "").strip() or S.TOOLS_SHELL_CWD
    try:
        os.makedirs(cwd, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": f"cwd not writable: {type(e).__name__}: {e}"}

    try:
        cp = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=S.TOOLS_GIT_TIMEOUT_SEC,
            check=False,
        )
        return {
            "ok": True,
            "returncode": cp.returncode,
            "stdout": cp.stdout[-20000:],
            "stderr": cp.stderr[-20000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {S.TOOLS_GIT_TIMEOUT_SEC}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_noop(args: Dict[str, Any]) -> Dict[str, Any]:
    """A safe tool for end-to-end verification.

    Always succeeds and echoes a single string back.
    """

    text = args.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        return {"ok": False, "error": "text must be a string"}
    return {"ok": True, "text": text}


TOOL_IMPL = {
    "noop": tool_noop,
    "shell": tool_shell,
    "read_file": tool_read_file,
    "read_file_lines": tool_read_file_lines,
    "list_dir": tool_list_dir,
    "search_files": tool_search_files,
    "search_text": tool_search_text,
    "write_file": tool_write_file,
    "http_fetch": tool_http_fetch,
    "http_fetch_local": tool_http_fetch_local,
    "web_browse": tool_web_browse,
    "current_time": tool_current_time,
    "tool_manifest": tool_tool_manifest,
    "git": tool_git,
    "system_info": tool_system_info,
    "cluster_resources": tool_cluster_resources,
    "models_refresh": tool_models_refresh,
    "coding_model_integration": tool_coding_model_integration,
    "coding_task_create": tool_coding_task_create,
    "nexus_agent_api": tool_nexus_agent_api,
    "coding_task_monitor": tool_coding_task_monitor,
    "coding_task_inspect": tool_coding_task_inspect,
    "coding_task_intervene": tool_coding_task_intervene,
    "coding_task_notify": tool_coding_task_notify,
    "agent_task_create": agent_tasks.create_task,
    "agent_task_list": agent_tasks.list_tasks,
    "agent_task_cancel": agent_tasks.cancel_task,
    "memory_v2_upsert": lambda args: memory_v2.upsert(
        db_path=S.MEMORY_DB_PATH,
        embed=_embed_text_sync,
        text=str(args.get("text") or ""),
        mtype=str(args.get("type") or "fact"),
        source=str(args.get("source") or "user"),
        meta=args.get("meta") if isinstance(args.get("meta"), dict) else None,
        mid=str(args.get("id") or "") or None,
        ts=int(args.get("ts")) if args.get("ts") is not None else None,
    ),
    "memory_v2_search": lambda args: memory_v2.search(
        db_path=S.MEMORY_DB_PATH,
        embed=_embed_text_sync,
        query=str(args.get("query") or ""),
        k=int(args.get("top_k") or 6),
        min_sim=float(args.get("min_sim") or 0.25),
        types=args.get("types") if isinstance(args.get("types"), list) else None,
        sources=args.get("sources") if isinstance(args.get("sources"), list) else None,
        max_age_sec=int(args.get("max_age_sec")) if args.get("max_age_sec") is not None else None,
        include_compacted=bool(args.get("include_compacted") or False),
    ),
    "memory_v2_list": lambda args: memory_v2.list_items(
        db_path=S.MEMORY_DB_PATH,
        types=args.get("types") if isinstance(args.get("types"), list) else None,
        sources=args.get("sources") if isinstance(args.get("sources"), list) else None,
        since_ts=int(args.get("since_ts")) if args.get("since_ts") is not None else None,
        max_age_sec=int(args.get("max_age_sec")) if args.get("max_age_sec") is not None else None,
        limit=int(args.get("limit") or 50),
        include_compacted=bool(args.get("include_compacted") or False),
    ),
    "memory_v2_delete": lambda args: memory_v2.delete_items(
        db_path=S.MEMORY_DB_PATH,
        ids=args.get("ids") if isinstance(args.get("ids"), list) else [],
    ),
}


def allowed_tool_names_for_policy(policy: dict | None) -> set[str]:
    pol = policy if isinstance(policy, dict) else {}
    allowed: set[str] = {"noop", "current_time", "tool_manifest"}

    raw = (pol.get("tools_allowlist") or S.TOOLS_ALLOWLIST or "").strip()
    if raw:
        allowed.update({p.strip() for p in raw.split(",") if p.strip()})
        return allowed

    allow_shell = bool(pol.get("tools_allow_shell", S.TOOLS_ALLOW_SHELL))
    allow_fs = bool(pol.get("tools_allow_fs", S.TOOLS_ALLOW_FS))
    allow_http = bool(pol.get("tools_allow_http_fetch", S.TOOLS_ALLOW_HTTP_FETCH))
    allow_web_browse = bool(pol.get("tools_allow_web_browse", getattr(S, "TOOLS_ALLOW_WEB_BROWSE", True)))
    allow_git = bool(pol.get("tools_allow_git", S.TOOLS_ALLOW_GIT))

    if allow_shell:
        allowed.add("shell")
    if allow_fs:
        allowed.update({"read_file", "read_file_lines", "list_dir", "search_files", "search_text", "write_file"})
    if allow_http:
        allowed.add("http_fetch")
        allowed.add("http_fetch_local")
    if allow_web_browse:
        allowed.add("web_browse")
    if allow_git:
        allowed.add("git")

    if bool(pol.get("tools_allow_system_info", getattr(S, "TOOLS_ALLOW_SYSTEM_INFO", False))):
        allowed.add("system_info")
    if bool(pol.get("tools_allow_cluster_resources", getattr(S, "TOOLS_ALLOW_CLUSTER_RESOURCES", True))):
        allowed.add("cluster_resources")
    if bool(pol.get("tools_allow_models_refresh", getattr(S, "TOOLS_ALLOW_MODELS_REFRESH", False))):
        allowed.add("models_refresh")
    if bool(getattr(S, "CODING_ENABLED", True)) and bool(pol.get("tools_allow_coding_model_integration", True)):
        allowed.add("coding_model_integration")
    if bool(getattr(S, "CODING_ENABLED", True)) and bool(pol.get("tools_allow_coding_task_create", True)):
        allowed.add("coding_task_create")
    if bool(getattr(S, "CODING_ENABLED", True)) and bool(pol.get("tools_allow_coding_supervision", True)):
        allowed.update({"coding_task_monitor", "coding_task_inspect", "coding_task_intervene", "coding_task_notify"})
    if bool(getattr(S, "CODING_ENABLED", True)) and bool(pol.get("tools_allow_agent_api", True)):
        allowed.add("nexus_agent_api")
    return allowed


def _allowed_tool_names() -> set[str]:
    """Default/global allowlist.

    Kept as a stable seam for tests (monkeypatch) and internal callers that
    don't have request context.
    """

    return allowed_tool_names_for_policy(None)


def _allowed_tool_names_for_req(req: Request) -> set[str]:
    pol = _token_policy(req)
    if not pol:
        return _allowed_tool_names()
    return allowed_tool_names_for_policy(pol)


def is_tool_allowed(name: str) -> bool:
    return name in _allowed_tool_names()


TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "noop": {
        "name": "noop",
        "version": "1",
        "description": "No-op tool for end-to-end verification.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
    },
    "shell": {
        "name": "shell",
        "version": "1",
        "description": "Run a command locally (no shell=True).",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "Command string to execute."}},
            "required": ["cmd"],
            "additionalProperties": False,
        },
    },
    "read_file": {
        "name": "read_file",
        "version": "1",
        "description": "Read a local text file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "read_file_lines": {
        "name": "read_file_lines",
        "version": "1",
        "description": "Read a bounded line range from a local text file under the configured filesystem roots.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "list_dir": {
        "name": "list_dir",
        "version": "1",
        "description": "List files and directories under the configured filesystem roots.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "include_hidden": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "search_files": {
        "name": "search_files",
        "version": "1",
        "description": "Find files or directories by glob pattern under the configured filesystem roots.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer"},
                "max_entries": {"type": "integer"},
                "include_hidden": {"type": "boolean"},
                "include_dirs": {"type": "boolean"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    "search_text": {
        "name": "search_text",
        "version": "1",
        "description": "Search text files for a literal string under the configured filesystem roots.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "string"},
                "file_pattern": {"type": "string"},
                "case_sensitive": {"type": "boolean"},
                "limit": {"type": "integer"},
                "context_lines": {"type": "integer"},
                "max_files": {"type": "integer"},
                "max_total_bytes": {"type": "integer"},
                "include_hidden": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "name": "write_file",
        "version": "1",
        "description": "Write a local text file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "current_time": {
        "name": "current_time",
        "version": "1",
        "description": "Return the current UTC time for scheduling, freshness checks, and timestamped reasoning.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "tool_manifest": {
        "name": "tool_manifest",
        "version": "1",
        "description": "Return the Nexus tools currently available to this agent, grouped by category with usage guidance.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_parameters": {"type": "boolean"},
                "category": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "git": {
        "name": "git",
        "version": "1",
        "description": "Run a limited set of git subcommands in a configured repo directory.",
        "parameters": {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"}}},
            "required": ["args"],
            "additionalProperties": False,
        },
    },
    "http_fetch": {
        "name": "http_fetch",
        "version": "1",
        "description": "Fetch a URL via GET with host allowlist and size limits.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    "http_fetch_local": {
        "name": "http_fetch_local",
        "version": "1",
        "description": "Fetch a URL via GET, restricted to localhost only.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    "web_browse": {
        "name": "web_browse",
        "version": "1",
        "description": "Fetch a public HTTP/HTTPS page and return readable text, title, links, and response metadata. Blocks localhost/private-network targets unless explicitly enabled.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_bytes": {"type": "integer"},
                "timeout_sec": {"type": "number"},
                "max_redirects": {"type": "integer"},
                "extract_links": {"type": "boolean"},
                "include_html": {"type": "boolean"},
                "user_agent": {"type": "string"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    "system_info": {
        "name": "system_info",
        "version": "1",
        "description": "Return non-sensitive runtime and feature information.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "cluster_resources": {
        "name": "cluster_resources",
        "version": "1",
        "description": "Return the merged Nexus Resources snapshot, including hosts, RAM/disk/CPU/GPU/VRAM details when reported, core services, control-plane status, and backend availability.",
        "parameters": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean"},
                "include_sources": {"type": "boolean"}
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "models_refresh": {
        "name": "models_refresh",
        "version": "1",
        "description": "Ping upstream model endpoints to confirm reachability.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "coding_model_integration": {
        "name": "coding_model_integration",
        "version": "1",
        "description": "Create a Nexus coding workspace for integrating a HuggingFace model into a backend, with containerized scaffolding when appropriate and host-native MLX scaffolding otherwise.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "HuggingFace owner/model id or huggingface.co model URL."},
                "repo_url": {"type": "string", "description": "Target GitHub repository URL where the generated integration workspace will be pushed."},
                "preferred_runtime": {"type": "string", "enum": ["auto", "mlx", "vllm", "transformers"]},
                "route_kind": {"type": "string", "enum": ["chat", "embeddings", "images", "tts", "ocr", "video", "music", "json"]},
                "service_name": {"type": "string"},
                "base_branch": {"type": "string"},
                "branch_name": {"type": "string"},
                "prompt": {"type": "string"},
                "git_token": {"type": "string", "description": "Optional GitHub token override used to create or push the target repository."},
                "coding_model": {"type": "string"},
                "auto_run": {"type": "boolean"},
                "auto_commit": {"type": "boolean"},
                "commit_message": {"type": "string"}
            },
            "required": ["model", "repo_url"],
            "additionalProperties": False,
        },
    },
    "coding_task_create": {
        "name": "coding_task_create",
        "version": "1",
        "description": (
            "Create a general Nexus coding workspace for a repository and implementation prompt. "
            "Optionally queue the coding agent after the workspace is created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "Target Git repository URL. Defaults to the configured Nexus coding repository when omitted.",
                },
                "base_branch": {
                    "type": "string",
                    "description": "Branch to clone from. Defaults to the configured coding base branch.",
                },
                "branch_name": {"type": "string", "description": "Branch name to create for the workspace."},
                "prompt": {"type": "string", "description": "Implementation prompt for the coding workspace."},
                "git_token": {
                    "type": "string",
                    "description": "Optional Git token override used for clone and later push operations.",
                },
                "coding_model": {"type": "string", "description": "Coding model alias to use when auto_run is enabled."},
                "auto_run": {"type": "boolean", "description": "If true, queue the coding agent immediately after workspace creation."},
                "auto_commit": {
                    "type": "boolean",
                    "description": "Deprecated compatibility field. Successful runs are always committed by the Nexus controller.",
                },
                "commit_message": {
                    "type": "string",
                    "description": "Optional commit message used when auto_commit is enabled.",
                },
                "push_on_success": {"type": "boolean", "description": "Push the committed feature branch after a successful run."},
                "draft_pr_on_success": {"type": "boolean", "description": "Push and open a draft pull request after a successful run."},
                "pr_title": {"type": "string"},
                "pr_body": {"type": "string"},
                "max_cycles": {"type": "integer", "minimum": 4, "maximum": 1000},
                "max_runtime_sec": {"type": "integer", "minimum": 60, "maximum": 86400},
                "context_reset_cycles": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    "nexus_agent_api": {
        "name": "nexus_agent_api",
        "version": "1",
        "description": (
            "Use the authenticated Nexus Agent API to manage coding workspaces, tasks, execution, and artifacts. "
            "Parameters mirror REST request or query fields; binary artifact content is base64."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": list(AGENT_API_OPERATIONS)},
                "workspace_id": {"type": ["string", "null"]},
                "task_id": {"type": ["string", "null"]},
                "parameters": {"type": "object"},
            },
            "required": ["operation", "workspace_id", "task_id", "parameters"],
            "additionalProperties": False,
        },
    },
    "coding_task_monitor": {
        "name": "coding_task_monitor",
        "version": "1",
        "description": "Inspect Nexus coding workspaces, classify paused/stopped/stalled/failed runs, and return bounded safe actions for recovery.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum coding tasks to inspect."},
                "only_attention": {"type": "boolean", "description": "Return only tasks that currently need attention."},
                "stalled_after_sec": {"type": "number", "description": "Age threshold for classifying a running coding task as stalled."}
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "coding_task_inspect": {
        "name": "coding_task_inspect",
        "version": "1",
        "description": "Inspect one coding workspace in detail, including recent events, change counts, attention reasons, and safe recovery actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Coding task id such as code_abcdef123456."},
                "stalled_after_sec": {"type": "number", "description": "Age threshold for classifying a running coding task as stalled."}
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    "coding_task_intervene": {
        "name": "coding_task_intervene",
        "version": "1",
        "description": "Take a recovery action on a coding workspace: resume a paused/stopped run, send guidance, guide and resume, or request pause.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Coding task id such as code_abcdef123456."},
                "action": {"type": "string", "enum": ["resume", "guidance", "guide_and_resume", "pause"]},
                "message": {"type": "string", "description": "Guidance message for guidance or guide_and_resume. Optional prompt override for resume."},
                "actor": {"type": "string", "description": "Actor label recorded in workspace guidance and run events."}
            },
            "required": ["task_id", "action"],
            "additionalProperties": False,
        },
    },
    "coding_task_notify": {
        "name": "coding_task_notify",
        "version": "1",
        "description": "Send a structured Telegram alert for a noteworthy coding workspace update, with cooldown-based deduplication per workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Coding task id such as code_abcdef123456."},
                "summary": {"type": "string", "description": "Short user-facing update to send."},
                "severity": {"type": "string", "enum": ["info", "warning", "critical"], "description": "Severity label included in the Telegram alert."},
                "dedupe_key": {"type": "string", "description": "Stable dedupe key for repeated noteworthy alerts about the same issue."},
                "cooldown_sec": {"type": "integer", "description": "Minimum seconds before another alert with the same dedupe_key may be sent."},
                "actor": {"type": "string", "description": "Actor label recorded in workspace events."}
            },
            "required": ["task_id", "summary"],
            "additionalProperties": False,
        },
    },
    "agent_task_create": {
        "name": "agent_task_create",
        "version": "1",
        "description": (
            "Create a durable scheduled Nexus agent task. Supports one-shot countdowns via delay_seconds, "
            "absolute run_at times, recurring interval_seconds, or five-field cron expressions evaluated in UTC. "
            "Due tasks run through AgentRuntimeV1 and are recorded for later inspection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Prompt the agent should handle when the task fires."},
                "title": {"type": "string", "description": "Short human-readable task title."},
                "agent": {"type": "string", "description": "Agent spec id to run; defaults to default."},
                "run_at": {"type": "string", "description": "ISO-8601 UTC time or Unix timestamp for an absolute one-shot or first interval run."},
                "delay_seconds": {"type": "integer", "description": "Countdown delay for a one-shot task or first interval run."},
                "interval_seconds": {"type": "integer", "description": "Repeat every N seconds; minimum 60 seconds."},
                "cron": {"type": "string", "description": "Five-field cron expression in UTC: minute hour day month weekday."},
                "max_runs": {"type": "integer", "description": "Optional maximum run count for recurring tasks."},
                "metadata": {"type": "object", "description": "Optional structured metadata for clients."},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    "agent_task_list": {
        "name": "agent_task_list",
        "version": "1",
        "description": "List durable scheduled Nexus agent tasks, including next run, last run, and last result summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Optional status filter: enabled, running, completed, cancelled, or error."},
                "limit": {"type": "integer", "description": "Maximum tasks to return, 1-200."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "agent_task_cancel": {
        "name": "agent_task_cancel",
        "version": "1",
        "description": "Cancel a scheduled Nexus agent task by id. Running tasks are allowed to finish; future runs are disabled.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Task id returned by agent_task_create or agent_task_list."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    "memory_v2_upsert": {
        "name": "memory_v2_upsert",
        "version": "1",
        "description": "Upsert a memory v2 item (typed, embedded, stored in SQLite).",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["fact", "preference", "project", "ephemeral"]},
                "text": {"type": "string"},
                "source": {"type": "string", "enum": ["user", "system", "tool"]},
                "meta": {"type": "object"},
                "id": {"type": "string"},
                "ts": {"type": "integer"},
            },
            "required": ["type", "text"],
            "additionalProperties": False,
        },
    },
    "memory_v2_search": {
        "name": "memory_v2_search",
        "version": "1",
        "description": "Semantic search memory v2 by query embedding.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "min_sim": {"type": "number"},
                "types": {"type": "array", "items": {"type": "string"}},
                "sources": {"type": "array", "items": {"type": "string"}},
                "max_age_sec": {"type": "integer"},
                "include_compacted": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "memory_v2_list": {
        "name": "memory_v2_list",
        "version": "1",
        "description": "List memory v2 items with optional filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "types": {"type": "array", "items": {"type": "string"}},
                "sources": {"type": "array", "items": {"type": "string"}},
                "since_ts": {"type": "integer"},
                "max_age_sec": {"type": "integer"},
                "limit": {"type": "integer"},
                "include_compacted": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "memory_v2_delete": {
        "name": "memory_v2_delete",
        "version": "1",
        "description": "Delete memory v2 items by id.",
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ids"],
            "additionalProperties": False,
        },
    },
}


def run_tool_call(
    name: str,
    arguments_json: str,
    *,
    allowed_tools: set[str] | None = None,
    caller: AgentToolCaller | None = None,
) -> Dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        return {
            "ok": False,
            "error": "tool name must be a non-empty string",
            "error_type": "invalid_request",
            "error_message": "tool name must be a non-empty string",
        }
    name = name.strip()

    if allowed_tools is not None:
        if name not in allowed_tools:
            return {
                "ok": False,
                "error": f"unknown tool: {name}",
                "error_type": "unknown_tool",
                "error_message": f"unknown tool: {name}",
            }
    elif not is_tool_allowed(name):
        # Fail closed, and avoid revealing undeclared tools.
        return {
            "ok": False,
            "error": f"unknown tool: {name}",
            "error_type": "unknown_tool",
            "error_message": f"unknown tool: {name}",
        }

    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception:
        return {
            "ok": False,
            "error": "tool arguments must be valid JSON",
            "error_type": "invalid_arguments",
            "error_message": "tool arguments must be valid JSON",
        }

    if not isinstance(args, dict):
        return {
            "ok": False,
            "error": "tool arguments must be a JSON object",
            "error_type": "invalid_arguments",
            "error_message": "tool arguments must be a JSON object",
        }

    try:
        # Delegate to the same deterministic executor used by /v1/tools.
        return _execute_tool(name, args, allowed_tools=allowed_tools, caller=caller)
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict):
            msg = detail.get("error") or "tool call failed"
            out: Dict[str, Any] = {
                "ok": False,
                "error": str(msg),
                "error_type": "tool_call_failed",
                "error_message": str(msg),
            }
            issues = detail.get("issues")
            if isinstance(issues, list):
                out["issues"] = issues
            return out
        return {
            "ok": False,
            "error": str(detail),
            "error_type": "tool_call_failed",
            "error_message": str(detail),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "error_type": type(e).__name__,
            "error_message": str(e),
        }


def _execute_tool(
    name: str,
    args: Dict[str, Any],
    *,
    allowed_tools: set[str] | None = None,
    caller: AgentToolCaller | None = None,
) -> Dict[str, Any]:
    """Execute a tool with validation + replay ID + deterministic logging."""

    sem = _tools_concurrency_sem()
    try:
        timeout_sec = float(getattr(S, "TOOLS_CONCURRENCY_TIMEOUT_SEC", 5.0))
    except Exception:
        timeout_sec = 5.0

    acquired = False
    try:
        acquired = sem.acquire(timeout=timeout_sec)
    except Exception:
        acquired = sem.acquire(blocking=True)

    if not acquired:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "tool capacity exceeded",
                "error_type": "rate_limited",
                "error_message": "tool capacity exceeded",
            },
        )

    if allowed_tools is not None:
        allowed = name in allowed_tools
    else:
        allowed = is_tool_allowed(name)

    if not allowed:
        # Fail closed, and avoid revealing undeclared tools.
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"unknown tool: {name}",
                "error_type": "unknown_tool",
                "error_message": f"unknown tool: {name}",
            },
        )

    sch, reg_def, _src = _resolve_declared_tool(name)
    if not (isinstance(sch, dict) and isinstance(sch.get("parameters"), dict) and isinstance(sch.get("version"), str)):
        # No implicit discovery: tools must be explicitly declared and versioned.
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"undeclared tool: {name}",
                "error_type": "undeclared_tool",
                "error_message": f"undeclared tool: {name}",
            },
        )

    errs = _validate_against_schema(sch["parameters"], args)
    if errs:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid tool arguments",
                "error_type": "invalid_arguments",
                "error_message": "invalid tool arguments",
                "issues": errs,
            },
        )

    version = str(sch["version"])
    req_hash = _request_hash(tool=name, version=version, args=args)

    replay_id = new_id("tool")
    ts = now_unix()
    t0 = time.monotonic()

    # Best-effort CPU accounting.
    cpu_self_0 = time.process_time()
    cpu_children_0 = None
    if resource is not None:
        try:
            ru = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_children_0 = float(ru.ru_utime) + float(ru.ru_stime)
        except Exception:
            cpu_children_0 = None

    out: Dict[str, Any]
    try:
        try:
            if reg_def and isinstance(reg_def.get("exec"), dict) and reg_def["exec"].get("type") == "subprocess":
                out = _normalize_tool_result(_run_subprocess_tool(exec_spec=reg_def["exec"], args=args))
            else:
                impl_args = dict(args)
                if name == "tool_manifest" and allowed_tools is not None:
                    impl_args["__allowed_tools"] = sorted(allowed_tools)
                if name == "nexus_agent_api":
                    impl_args["__caller"] = caller
                out = _normalize_tool_result(TOOL_IMPL[name](impl_args))
        except Exception as e:
            out = _normalize_tool_result({"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        try:
            sem.release()
        except Exception:
            pass

    _attach_stdout_json(out)
    _normalize_tool_error(out)

    dur_ms = (time.monotonic() - t0) * 1000.0

    cpu_self_1 = time.process_time()
    cpu_children_1 = None
    if resource is not None:
        try:
            ru = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_children_1 = float(ru.ru_utime) + float(ru.ru_stime)
        except Exception:
            cpu_children_1 = None

    # Envelope fields (some may be stubbed / best-effort).
    tool_runtime_ms = round(dur_ms, 1)
    tool_cpu_ms: float | None
    try:
        cpu = max(0.0, float(cpu_self_1 - cpu_self_0))
        if cpu_children_0 is not None and cpu_children_1 is not None:
            cpu += max(0.0, float(cpu_children_1 - cpu_children_0))
        tool_cpu_ms = round(cpu * 1000.0, 1)
    except Exception:
        tool_cpu_ms = None

    tool_io_bytes = 0
    if isinstance(out, dict):
        # Prefer tool-provided byte counts (file/network I/O), otherwise fall back
        # to stdout/stderr size for subprocess-backed tools.
        hinted = out.pop("__io_bytes", None)
        if isinstance(hinted, int) and hinted >= 0:
            tool_io_bytes = hinted
        else:
            so = out.get("stdout")
            se = out.get("stderr")
            if isinstance(so, str):
                tool_io_bytes += len(so.encode("utf-8", errors="ignore"))
            if isinstance(se, str):
                tool_io_bytes += len(se.encode("utf-8", errors="ignore"))

    logged_args = args
    if name == "nexus_agent_api" and str(args.get("operation") or "") == "upload_artifact":
        logged_args = dict(args)
        parameters = dict(args.get("parameters") or {}) if isinstance(args.get("parameters"), dict) else {}
        encoded = parameters.get("content_base64")
        if isinstance(encoded, str):
            parameters["content_base64"] = f"[OMITTED {len(encoded)} base64 characters]"
        logged_args["parameters"] = parameters
    event = {
        "ts": ts,
        "replay_id": replay_id,
        "request_hash": req_hash,
        "tool": name,
        "version": version,
        "ok": bool(out.get("ok")) if isinstance(out, dict) else False,
        "tool_runtime_ms": tool_runtime_ms,
        "tool_cpu_ms": tool_cpu_ms,
        "tool_io_bytes": tool_io_bytes,
        "args": _truncate(logged_args, max_chars=10_000),
        "result": _truncate(out, max_chars=20_000),
    }

    try:
        _log_tool_event(replay_id, event)
    except Exception:
        pass

    # Best-effort metrics.
    try:
        if getattr(S, "METRICS_ENABLED", True):
            metrics.observe_tool(name, bool(out.get("ok")), float(tool_runtime_ms))
    except Exception:
        pass

    # Backward-compatible response shape, with replay_id attached.
    if isinstance(out, dict):
        return {
            "replay_id": replay_id,
            "request_hash": req_hash,
            "tool_runtime_ms": tool_runtime_ms,
            "tool_cpu_ms": tool_cpu_ms,
            "tool_io_bytes": tool_io_bytes,
            **out,
        }
    return {
        "replay_id": replay_id,
        "request_hash": req_hash,
        "tool_runtime_ms": tool_runtime_ms,
        "tool_cpu_ms": tool_cpu_ms,
        "tool_io_bytes": tool_io_bytes,
        "ok": False,
        "error": "invalid tool result",
    }


@router.get("/v1/tools")
async def v1_tools_list(req: Request):
    require_bearer(req)
    _rate_limit(req)
    allowed = sorted(_allowed_tool_names_for_req(req))
    data = []
    for name in allowed:
        sch, _reg_def, src = _resolve_declared_tool(name)
        if sch:
            data.append(
                {
                    "name": sch["name"],
                    "version": sch.get("version", ""),
                    "description": sch["description"],
                    "parameters": sch["parameters"],
                    "declared": True,
                    "source": src,
                }
            )
        else:
            # No implicit discovery: if allowed but not declared, show it explicitly as missing.
            _warn_allowlisted_undeclared(name)
            data.append(
                {
                    "name": name,
                    "version": "",
                    "description": "(undeclared)",
                    "parameters": {"type": "object"},
                    "declared": False,
                    "source": "missing",
                }
            )
    return {"object": "list", "data": data}


@router.get("/v1/tools/replay/{replay_id}")
async def v1_tools_replay(req: Request, replay_id: str):
    """Fetch a previously logged tool invocation event.

    Prefers per-invocation file logs (TOOLS_LOG_DIR/{replay_id}.json). If not
    present, falls back to scanning the NDJSON log (TOOLS_LOG_PATH).
    """

    require_bearer(req)
    _rate_limit(req)
    rid = (replay_id or "").strip()
    if not rid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid replay id",
                "error_type": "invalid_request",
                "error_message": "replay_id must be a non-empty string",
            },
        )

    # Prefer per-invocation file
    try:
        p = os.path.join(_tools_log_dir(), f"{rid}.json")
        if os.path.exists(p):
            raw = Path(p).read_text(encoding="utf-8")
            return json.loads(raw)
    except Exception:
        pass

    # Fallback: scan NDJSON log for the last matching replay_id.
    try:
        path = _tools_log_path()
        if os.path.exists(path):
            last = None
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and obj.get("replay_id") == rid:
                        last = obj
            if isinstance(last, dict):
                return last
    except Exception:
        pass

    raise HTTPException(
        status_code=404,
        detail={
            "error": f"replay not found: {rid}",
            "error_type": "replay_not_found",
            "error_message": f"replay not found: {rid}",
        },
    )


@router.post("/v1/tools")
async def v1_tools_dispatch(req: Request):
    """Dispatcher endpoint.

    Body:
      {"name": "read_file", "arguments": {...}}
    """

    require_bearer(req)
    _rate_limit(req)
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid request body",
                "error_type": "invalid_request",
                "error_message": "body must be an object",
            },
        )
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid request body",
                "error_type": "invalid_request",
                "error_message": "name must be a non-empty string",
            },
        )
    args = body.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid request body",
                "error_type": "invalid_request",
                "error_message": "arguments must be an object",
            },
        )
    return await run_in_threadpool(
        _execute_tool,
        name.strip(),
        args,
        allowed_tools=_allowed_tool_names_for_req(req),
        caller=agent_tool_caller_from_request(req),
    )


@router.post("/v1/tools/{name}")
async def v1_tools_exec(req: Request, name: str):
    require_bearer(req)
    _rate_limit(req)
    body = await req.json()

    # Accept two forms for convenience:
    # 1) { "arguments": { ... } }  (explicit)
    # 2) { "prompt": "...", "duration": 30 } (shortcut where the body IS the arguments)
    if isinstance(body, dict) and "arguments" not in body:
        args = body
    else:
        tr = ToolExecRequest(**body)
        args = tr.arguments

    return await run_in_threadpool(
        _execute_tool,
        name,
        args,
        allowed_tools=_allowed_tool_names_for_req(req),
        caller=agent_tool_caller_from_request(req),
    )
