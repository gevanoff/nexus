from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict
from pathlib import Path
from urllib.parse import urlparse
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
from app.config import S
from app.models import ToolExecRequest
from app.openai_utils import new_id, now_unix


router = APIRouter()


log = logging.getLogger(__name__)


from app import metrics
from app import memory_v2
from app.upstreams import embed_text_for_memory


_REGISTRY_CACHE: dict[str, Any] = {"path": None, "mtime": None, "tools": {}}


_TOOLS_CONCURRENCY_SEM: threading.Semaphore | None = None


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


def tool_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if not S.TOOLS_ALLOW_FS:
        return {"ok": False, "error": "fs tool disabled"}
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty string"}
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

        max_bytes = int(S.TOOLS_FS_MAX_BYTES)
        with open(p, "rb") as f:
            data = f.read(max_bytes + 1)

        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        return {"ok": True, "path": str(p), "truncated": truncated, "content": text, "__io_bytes": len(data)}
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


def tool_models_refresh(args: Dict[str, Any]) -> Dict[str, Any]:
    if not getattr(S, "TOOLS_ALLOW_MODELS_REFRESH", False):
        return {"ok": False, "error": "models_refresh tool disabled"}

    out: Dict[str, Any] = {"ok": True, "upstreams": {}}
    timeout = float(getattr(S, "TOOLS_HTTP_TIMEOUT_SEC", 10))
    try:
        with httpx.Client(timeout=timeout) as client:
            try:
                r = client.get(f"{S.OLLAMA_BASE_URL}/api/tags")
                out["upstreams"]["ollama"] = {"ok": r.status_code == 200, "status": r.status_code}
            except Exception as e:
                out["ok"] = False
                out["upstreams"]["ollama"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            try:
                r = client.get(f"{S.MLX_BASE_URL}/models")
                out["upstreams"]["mlx"] = {"ok": r.status_code == 200, "status": r.status_code}
            except Exception as e:
                out["ok"] = False
                out["upstreams"]["mlx"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


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
    "write_file": tool_write_file,
    "http_fetch": tool_http_fetch,
    "http_fetch_local": tool_http_fetch_local,
    "git": tool_git,
    "system_info": tool_system_info,
    "models_refresh": tool_models_refresh,
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

    raw = (pol.get("tools_allowlist") or S.TOOLS_ALLOWLIST or "").strip()
    if raw:
        return {p.strip() for p in raw.split(",") if p.strip()}

    allowed: set[str] = set()
    # Always-available safe tool for verification.
    allowed.add("noop")

    allow_shell = bool(pol.get("tools_allow_shell", S.TOOLS_ALLOW_SHELL))
    allow_fs = bool(pol.get("tools_allow_fs", S.TOOLS_ALLOW_FS))
    allow_http = bool(pol.get("tools_allow_http_fetch", S.TOOLS_ALLOW_HTTP_FETCH))
    allow_git = bool(pol.get("tools_allow_git", S.TOOLS_ALLOW_GIT))

    if allow_shell:
        allowed.add("shell")
    if allow_fs:
        allowed.update({"read_file", "write_file"})
    if allow_http:
        allowed.add("http_fetch")
        allowed.add("http_fetch_local")
    if allow_git:
        allowed.add("git")

    if bool(pol.get("tools_allow_system_info", getattr(S, "TOOLS_ALLOW_SYSTEM_INFO", False))):
        allowed.add("system_info")
    if bool(pol.get("tools_allow_models_refresh", getattr(S, "TOOLS_ALLOW_MODELS_REFRESH", False))):
        allowed.add("models_refresh")
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


def run_tool_call(name: str, arguments_json: str, *, allowed_tools: set[str] | None = None) -> Dict[str, Any]:
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
        return _execute_tool(name, args, allowed_tools=allowed_tools)
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


def _execute_tool(name: str, args: Dict[str, Any], *, allowed_tools: set[str] | None = None) -> Dict[str, Any]:
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
                out = _normalize_tool_result(TOOL_IMPL[name](args))
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
        "args": _truncate(args, max_chars=10_000),
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
    )
