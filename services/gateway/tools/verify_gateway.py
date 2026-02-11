#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
from urllib.parse import urlparse
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_TLS_CONTEXT: ssl.SSLContext | None = None


def _maybe_reexec_into_gateway_venv() -> None:
    """Re-exec into the gateway venv python when available.

    This mirrors the deployment layout used by ai-infra on macOS/Linux.
    """

    if os.getenv("GATEWAY_SKIP_REEXEC") == "1":
        return

    candidates: list[str] = []
    override = (os.getenv("GATEWAY_VENV_PY") or "").strip()
    if override:
        candidates.append(override)

    candidates.extend(
        [
            "/var/lib/gateway/env/bin/python",
            "/var/lib/gateway/venv/bin/python",
        ]
    )

    try:
        for venv_py in candidates:
            if os.path.exists(venv_py) and os.path.realpath(sys.executable) != os.path.realpath(venv_py):
                env = dict(os.environ)
                env["GATEWAY_SKIP_REEXEC"] = "1"
                os.execve(venv_py, [venv_py, *sys.argv], env)
    except Exception:
        return


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return int(s.getsockname()[1])


def _derive_obs_url(base_url: str) -> str:
    override = (os.getenv("GATEWAY_OBS_URL") or "").strip()
    if override:
        return override

    obs_port = (os.getenv("OBSERVABILITY_PORT") or "8801").strip()
    try:
        obs_port_int = int(obs_port)
    except Exception:
        obs_port_int = 8801

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    return f"http://{host}:{obs_port_int}"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout_sec: float = 20.0,
    max_body_bytes: int = 200_000,
) -> tuple[int, dict[str, str], bytes]:
    body: bytes | None = None
    req_headers: dict[str, str] = dict(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req_headers.setdefault("content-type", "application/json")

    request = Request(url=url, data=body, headers=req_headers, method=method.upper())

    try:
        with urlopen(request, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read(max_body_bytes + 1)
            return status, resp_headers, data[:max_body_bytes]
    except HTTPError as e:
        status = int(getattr(e, "code", 0) or 0)
        try:
            data = e.read(max_body_bytes + 1)
        except Exception:
            data = b""
        return status, {k.lower(): v for k, v in getattr(e, "headers", {}).items()}, data[:max_body_bytes]
    except URLError as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")


def _json_from_bytes(b: bytes) -> object:
    if not b:
        raise ValueError("empty body")
    return json.loads(b.decode("utf-8"))


def _http_stream_until_done(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout_sec: float = 20.0,
    max_bytes: int = 128_000,
) -> tuple[bool, str]:
    body: bytes | None = None
    req_headers: dict[str, str] = dict(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req_headers.setdefault("content-type", "application/json")

    request = Request(url=url, data=body, headers=req_headers, method="POST")

    buf = bytearray()
    try:
        with urlopen(request, timeout=timeout_sec, context=_TLS_CONTEXT) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            if status != 200:
                return False, f"status={status}"
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"data: [DONE]" in buf:
                    return True, ""
                if len(buf) > max_bytes:
                    break
        return False, "did not observe 'data: [DONE]' within limit"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _wait_for_health(obs_url: str, token: str, *, timeout_sec: float = 15.0) -> None:
    headers = {"authorization": f"Bearer {token}"}
    deadline = time.time() + timeout_sec
    last_err: Optional[str] = None

    while time.time() < deadline:
        try:
            status, _h, body = _http_request("GET", obs_url.rstrip("/") + "/health", headers=headers, timeout_sec=2.5)
            if status == 200:
                return
            last_err = f"status={status} body={(body[:200] or b'').decode('utf-8', errors='replace')}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.25)

    raise RuntimeError(f"gateway did not become healthy: {last_err}")


def _run_pytest(*, cwd: str) -> CheckResult:
    try:
        import pytest  # noqa: F401
    except Exception:
        return CheckResult(
            name="pytest",
            ok=True,
            detail="skipped (pytest not installed; use --skip-pytest or install requirements-dev.txt)",
        )

    cp = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode == 0:
        return CheckResult(name="pytest", ok=True, detail=cp.stdout.strip())
    out = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return CheckResult(name="pytest", ok=False, detail=out.strip()[-8000:])


def _start_uvicorn(*, cwd: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]

    # If TLS cert/key paths are provided in env, forward them to uvicorn so it
    # can serve HTTPS directly (useful for simple local TLS testing).
    cert = env.get("GATEWAY_TLS_CERT_PATH") or os.getenv("GATEWAY_TLS_CERT_PATH")
    key = env.get("GATEWAY_TLS_KEY_PATH") or os.getenv("GATEWAY_TLS_KEY_PATH")
    if cert and key:
        argv.extend(["--ssl-certfile", cert, "--ssl-keyfile", key])

    # Avoid opening a new console window on Windows; harmless elsewhere.
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )


def _stop_process(proc: subprocess.Popen, *, timeout_sec: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout_sec)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_http_checks(*, base_url: str, obs_url: str, token: str, require_backend: bool, check_images: bool) -> list[CheckResult]:
    results: list[CheckResult] = []

    def ok(name: str, detail: str = "") -> None:
        results.append(CheckResult(name=name, ok=True, detail=detail))

    def bad(name: str, detail: str) -> None:
        results.append(CheckResult(name=name, ok=False, detail=detail))

    bearer = {"authorization": f"Bearer {token}"}

    # /health (GET + HEAD)
    try:
        status, _h, body = _http_request("GET", obs_url.rstrip("/") + "/health", headers=bearer, timeout_sec=10.0)
        if status == 200:
            ok("health_get")
        else:
            bad("health_get", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("health_get", f"{type(e).__name__}: {e}")
        return results

    try:
        status, _h, _body = _http_request("HEAD", obs_url.rstrip("/") + "/health", headers=bearer, timeout_sec=10.0)
        if status == 200:
            ok("health_head")
        else:
            bad("health_head", f"status={status}")
    except Exception as e:
        bad("health_head", f"{type(e).__name__}: {e}")

    # /metrics (local observability listener)
    try:
        status, _h, body = _http_request("GET", obs_url.rstrip("/") + "/metrics", headers=bearer, timeout_sec=10.0)
        if status == 200 and body.strip():
            ok("metrics")
        else:
            bad("metrics", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("metrics", f"{type(e).__name__}: {e}")

    # OpenAI-ish endpoints
    v1 = base_url.rstrip("/") + "/v1"

    def _check_images() -> CheckResult:
        try:
            # Default should be URL (policy: avoid b64 unless explicitly requested).
            payload = {"prompt": "verify_gateway images url", "size": "256x256", "n": 1}
            status, _h, body = _http_request(
                "POST",
                f"{v1}/images/generations",
                headers=bearer,
                json_body=payload,
                timeout_sec=120.0,
                max_body_bytes=2_000_000,
            )
            if status != 200:
                return CheckResult(
                    name="images_url",
                    ok=False,
                    detail=f"status={status} body={body[:400].decode('utf-8', errors='replace')}",
                )
            out = _json_from_bytes(body)
            data = out.get("data") if isinstance(out, dict) else None
            if not (isinstance(data, list) and data and isinstance(data[0], dict)):
                return CheckResult(name="images_url", ok=False, detail="missing data[0]")
            if "url" not in data[0]:
                return CheckResult(name="images_url", ok=False, detail="expected data[0].url")
            if "b64_json" in data[0]:
                return CheckResult(name="images_url", ok=False, detail="unexpected data[0].b64_json")

            # Explicit b64_json should return PNG-ish bytes.
            payload2 = {"prompt": "verify_gateway images b64", "size": "256x256", "n": 1, "response_format": "b64_json"}
            status, _h, body = _http_request(
                "POST",
                f"{v1}/images/generations",
                headers=bearer,
                json_body=payload2,
                timeout_sec=120.0,
                max_body_bytes=8_000_000,
            )
            if status != 200:
                return CheckResult(
                    name="images_b64",
                    ok=False,
                    detail=f"status={status} body={body[:400].decode('utf-8', errors='replace')}",
                )
            out2 = _json_from_bytes(body)
            data2 = out2.get("data") if isinstance(out2, dict) else None
            if not (isinstance(data2, list) and data2 and isinstance(data2[0], dict) and isinstance(data2[0].get("b64_json"), str)):
                return CheckResult(name="images_b64", ok=False, detail="missing data[0].b64_json")
            raw = base64.b64decode(data2[0]["b64_json"].encode("ascii"))
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                return CheckResult(name="images", ok=True, detail="url default + b64_json PNG OK")

            # Mock backend returns SVG; allow it unless appliance/require-backend mode.
            head = raw[:200].lstrip()
            if head.startswith(b"<svg") or head.startswith(b"<?xml"):
                if require_backend:
                    return CheckResult(name="images", ok=False, detail="got SVG placeholder (backend required)")
                return CheckResult(name="images", ok=True, detail="got SVG placeholder (IMAGES_BACKEND=mock)")

            return CheckResult(name="images", ok=False, detail="b64 did not decode to PNG/SVG")
        except Exception as e:
            return CheckResult(name="images", ok=False, detail=f"{type(e).__name__}: {e}")

    if check_images:
        results.append(_check_images())

    try:
        status, _h, body = _http_request("GET", v1 + "/models", headers=bearer)
        if status == 200:
            ok("models")
        else:
            bad("models", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("models", f"{type(e).__name__}: {e}")

    tool_names: set[str] = set()

    # Tool bus listing should be available regardless of tool enables.
    try:
        status, _h, body = _http_request("GET", v1 + "/tools", headers=bearer)
        if status == 200:
            ok("tools_list")
            try:
                payload = _json_from_bytes(body)
                data = payload.get("data") if isinstance(payload, dict) else None
                tool_names = {x.get("name") for x in data if isinstance(x, dict)} if isinstance(data, list) else set()
                if "noop" in tool_names:
                    ok("tools_has_noop")
                else:
                    bad("tools_has_noop", "noop tool missing from /v1/tools (expected built-in safe tool)")
            except Exception as e:
                bad("tools_has_noop", f"parse error: {type(e).__name__}: {e}")
        else:
            bad("tools_list", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("tools_list", f"{type(e).__name__}: {e}")

    # Tool execution + replay (safe, deterministic)
    replay_id: Optional[str] = None
    try:
        status, _h, body = _http_request("POST", v1 + "/tools/noop", headers=bearer, json_body={"arguments": {"text": "verify"}})
        if status == 200:
            try:
                payload = _json_from_bytes(body)
                if isinstance(payload, dict) and payload.get("ok") is True:
                    ok("tool_exec_noop")
                    rid = payload.get("replay_id")
                    replay_id = rid if isinstance(rid, str) and rid.strip() else None
                else:
                    bad("tool_exec_noop", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
            except Exception as e:
                bad("tool_exec_noop", f"parse error: {type(e).__name__}: {e}")
        else:
            bad("tool_exec_noop", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("tool_exec_noop", f"{type(e).__name__}: {e}")

    # Dispatcher execution path
    try:
        status, _h, body = _http_request("POST", v1 + "/tools", headers=bearer, json_body={"name": "noop", "arguments": {"text": "verify"}})
        if status == 200:
            try:
                payload = _json_from_bytes(body)
                if isinstance(payload, dict) and payload.get("ok") is True:
                    ok("tool_dispatch_noop")
                else:
                    bad("tool_dispatch_noop", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
            except Exception as e:
                bad("tool_dispatch_noop", f"parse error: {type(e).__name__}: {e}")
        else:
            bad("tool_dispatch_noop", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("tool_dispatch_noop", f"{type(e).__name__}: {e}")

    # Replay should succeed if tool logging is configured.
    if replay_id:
        try:
            status, _h, body = _http_request("GET", v1 + f"/tools/replay/{replay_id}", headers=bearer, timeout_sec=10.0)
            if status == 200:
                ok("tool_replay")
            else:
                bad(
                    "tool_replay",
                    f"status={status} body={body[:200].decode('utf-8', errors='replace')} (enable TOOLS_LOG_MODE/TOOLS_LOG_PATH/TOOLS_LOG_DIR)",
                )
        except Exception as e:
            bad("tool_replay", f"{type(e).__name__}: {e}")
    else:
        bad("tool_replay", "missing replay_id from tool execution")

    # http_fetch validation (only if allowlisted)
    if "http_fetch" in tool_names:
        try:
            status, _h, body = _http_request(
                "POST",
                v1 + "/tools/http_fetch",
                headers=bearer,
                json_body={"arguments": {"url": obs_url.rstrip("/") + "/health", "method": "GET"}},
            )
            if status == 200:
                try:
                    payload = _json_from_bytes(body)
                    if isinstance(payload, dict) and payload.get("ok") is True and int(payload.get("status", 0) or 0) == 200:
                        ok("tool_exec_http_fetch_health")
                    else:
                        bad("tool_exec_http_fetch_health", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
                except Exception as e:
                    bad("tool_exec_http_fetch_health", f"parse error: {type(e).__name__}: {e}")
            else:
                bad("tool_exec_http_fetch_health", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("tool_exec_http_fetch_health", f"{type(e).__name__}: {e}")
    else:
        ok("tool_exec_http_fetch_health", detail="skipped (http_fetch not allowlisted)")

    # http_fetch_local validation (only if allowlisted)
    if "http_fetch_local" in tool_names:
        try:
            status, _h, body = _http_request(
                "POST",
                v1 + "/tools/http_fetch_local",
                headers=bearer,
                json_body={"arguments": {"url": obs_url.rstrip("/") + "/health", "method": "GET"}},
            )
            if status == 200:
                try:
                    payload = _json_from_bytes(body)
                    if isinstance(payload, dict) and payload.get("ok") is True and int(payload.get("status", 0) or 0) == 200:
                        ok("tool_exec_http_fetch_local_health")
                    else:
                        bad("tool_exec_http_fetch_local_health", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
                except Exception as e:
                    bad("tool_exec_http_fetch_local_health", f"parse error: {type(e).__name__}: {e}")
            else:
                bad("tool_exec_http_fetch_local_health", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("tool_exec_http_fetch_local_health", f"{type(e).__name__}: {e}")
    else:
        ok("tool_exec_http_fetch_local_health", detail="skipped (http_fetch_local not allowlisted)")

    # system_info validation (only if allowlisted)
    if "system_info" in tool_names:
        try:
            status, _h, body = _http_request("POST", v1 + "/tools/system_info", headers=bearer, json_body={"arguments": {}})
            if status == 200:
                try:
                    payload = _json_from_bytes(body)
                    if isinstance(payload, dict) and payload.get("ok") is True:
                        ok("tool_exec_system_info")
                    else:
                        bad("tool_exec_system_info", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
                except Exception as e:
                    bad("tool_exec_system_info", f"parse error: {type(e).__name__}: {e}")
            else:
                bad("tool_exec_system_info", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("tool_exec_system_info", f"{type(e).__name__}: {e}")
    else:
        ok("tool_exec_system_info", detail="skipped (system_info not allowlisted)")

    # models_refresh validation (only if allowlisted)
    if "models_refresh" in tool_names:
        try:
            status, _h, body = _http_request("POST", v1 + "/tools/models_refresh", headers=bearer, json_body={"arguments": {}})
            if status == 200:
                try:
                    payload = _json_from_bytes(body)
                    if isinstance(payload, dict) and payload.get("ok") is True:
                        ok("tool_exec_models_refresh")
                    else:
                        bad("tool_exec_models_refresh", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
                except Exception as e:
                    bad("tool_exec_models_refresh", f"parse error: {type(e).__name__}: {e}")
            else:
                bad("tool_exec_models_refresh", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("tool_exec_models_refresh", f"{type(e).__name__}: {e}")
    else:
        ok("tool_exec_models_refresh", detail="skipped (models_refresh not allowlisted)")

    # Backend-dependent checks
    backends_ok = False
    try:
        status, _h, body = _http_request("GET", obs_url.rstrip("/") + "/health/upstreams", headers=bearer)
        if status == 200:
            try:
                payload = json.loads(body.decode("utf-8"))
                statuses = payload.get("upstreams") if isinstance(payload, dict) else None
                if isinstance(statuses, dict):
                    backends_ok = any((isinstance(v, dict) and v.get("ok") is True) for v in statuses.values())
            except Exception:
                backends_ok = False
            ok("health_upstreams", detail=("backend_ok" if backends_ok else "no_backend_ok"))
        else:
            bad("health_upstreams", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
    except Exception as e:
        bad("health_upstreams", f"{type(e).__name__}: {e}")

    if require_backend and not backends_ok:
        bad("backend_required", "no healthy upstreams reported")
        return results

    if backends_ok:
        # Embeddings
        try:
            status, _h, body = _http_request(
                "POST",
                v1 + "/embeddings",
                headers=bearer,
                json_body={"model": "default", "input": "Hello from appliance smoketest."},
                timeout_sec=30.0,
            )
            if status == 200:
                try:
                    payload = _json_from_bytes(body)
                    data = payload.get("data") if isinstance(payload, dict) else None
                    ok_shape = (
                        isinstance(data, list)
                        and len(data) >= 1
                        and isinstance(data[0], dict)
                        and isinstance((data[0].get("embedding") if data else None), list)
                    )
                    if ok_shape:
                        ok("embeddings")
                    else:
                        bad("embeddings", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
                except Exception as e:
                    bad("embeddings", f"parse error: {type(e).__name__}: {e}")
            else:
                bad("embeddings", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("embeddings", f"{type(e).__name__}: {e}")

        # Minimal /v1/responses compatibility (non-stream)
        try:
            status, _h, body = _http_request(
                "POST",
                v1 + "/responses",
                headers=bearer,
                json_body={"model": "fast", "input": "Say hi.", "stream": False},
            )
            if status == 200:
                try:
                    payload = _json_from_bytes(body)
                    if isinstance(payload, dict) and payload.get("object") == "response":
                        ok("responses_non_stream")
                    else:
                        bad("responses_non_stream", f"unexpected body: {body[:200].decode('utf-8', errors='replace')}")
                except Exception as e:
                    bad("responses_non_stream", f"parse error: {type(e).__name__}: {e}")
            else:
                bad("responses_non_stream", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("responses_non_stream", f"{type(e).__name__}: {e}")

        # /v1/responses streaming: verify we see the DONE marker.
        stream_headers = dict(bearer)
        stream_headers["accept"] = "text/event-stream"
        ok_stream, detail = _http_stream_until_done(
            v1 + "/responses",
            headers=stream_headers,
            json_body={"model": "fast", "input": "Count 1..3.", "stream": True},
        )
        if ok_stream:
            ok("responses_stream")
        else:
            bad("responses_stream", detail)

        # Memory UX endpoints (non-mutating check): export should either work (200) or be intentionally disabled (400).
        try:
            status, _h, body = _http_request(
                "GET",
                base_url.rstrip("/") + "/v1/memory/export?limit=1",
                headers=bearer,
                timeout_sec=10.0,
            )
            if status == 200:
                ok("memory_export")
            elif status == 400 and b"memory v2 disabled" in body.lower():
                ok("memory_export", detail="skipped (memory v2 disabled)")
            else:
                bad("memory_export", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("memory_export", f"{type(e).__name__}: {e}")

        # Non-streaming chat completion
        payload = {"model": "fast", "stream": False, "messages": [{"role": "user", "content": "Say hi."}]}
        try:
            status, _h, body = _http_request("POST", v1 + "/chat/completions", headers=bearer, json_body=payload)
            if status == 200:
                ok("chat_non_stream")
            else:
                bad("chat_non_stream", f"status={status} body={body[:200].decode('utf-8', errors='replace')}")
        except Exception as e:
            bad("chat_non_stream", f"{type(e).__name__}: {e}")

        # Streaming chat completion: verify we see the DONE marker.
        stream_headers = dict(bearer)
        stream_headers["accept"] = "text/event-stream"
        payload = {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Count 1..3."}]}
        ok_stream, detail = _http_stream_until_done(v1 + "/chat/completions", headers=stream_headers, json_body=payload)
        if ok_stream:
            ok("chat_stream")
        else:
            bad("chat_stream", detail)

    return results


def _print_results(results: list[CheckResult]) -> int:
    width = max((len(r.name) for r in results), default=10)
    failed = [r for r in results if not r.ok]

    for r in results:
        status = "OK" if r.ok else "FAIL"
        detail = ("" if not r.detail else f" - {r.detail}")
        print(f"{r.name.ljust(width)}  {status}{detail}")

    if failed:
        print(f"\nFAILED: {len(failed)} check(s)")
        return 1
    print("\nALL OK")
    return 0


def _env_gateway_token() -> str:
    tok = (os.getenv("GATEWAY_BEARER_TOKEN") or "").strip()
    if tok:
        return tok
    raw = (os.getenv("GATEWAY_BEARER_TOKENS") or "").strip()
    if raw:
        for part in raw.split(","):
            t = part.strip()
            if t:
                return t
    return ""


def main(argv: list[str]) -> int:
    _maybe_reexec_into_gateway_venv()

    p = argparse.ArgumentParser(description="Comprehensive verification for the Local AI Gateway.")
    p.add_argument(
        "--base-url",
        default="",
        help="If set, do HTTPS checks against an already-running gateway (e.g. https://127.0.0.1:8800).",
    )
    p.add_argument(
        "--obs-url",
        default="",
        help="Optional observability base URL for /health and /metrics (default: derive from base URL or $GATEWAY_OBS_URL).",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed local certs).",
    )
    p.add_argument(
        "--token",
        default="",
        help="Bearer token for /v1/* (default: $GATEWAY_BEARER_TOKEN or first of $GATEWAY_BEARER_TOKENS).",
    )
    p.add_argument("--skip-pytest", action="store_true", help="Skip running pytest.")
    p.add_argument(
        "--require-backend",
        action="store_true",
        help="Fail if no healthy upstream backend is available (otherwise backend-dependent checks are skipped).",
    )
    p.add_argument(
        "--appliance",
        action="store_true",
        help="Appliance smoke-test mode (implies --require-backend).",
    )
    p.add_argument(
        "--no-start",
        action="store_true",
        help="Do not auto-start uvicorn (requires --base-url).",
    )
    p.add_argument(
        "--check-images",
        action="store_true",
        help="Also smoke-test /v1/images/generations (url default + b64_json when requested).",
    )
    ns = p.parse_args(argv)

    token = (ns.token or "").strip() or _env_gateway_token()
    insecure = ns.insecure or (os.getenv("GATEWAY_TLS_INSECURE") or "").strip().lower() in {"1", "true", "yes", "on"}

    global _TLS_CONTEXT
    if insecure:
        _TLS_CONTEXT = ssl._create_unverified_context()
    else:
        _TLS_CONTEXT = ssl.create_default_context()

    if ns.appliance:
        ns.require_backend = True

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    results: list[CheckResult] = []

    if not ns.skip_pytest:
        results.append(_run_pytest(cwd=repo_root))

    proc: Optional[subprocess.Popen] = None
    base_url = (ns.base_url or "").strip()
    obs_url = (ns.obs_url or "").strip()

    if base_url and not token:
        results.append(
            CheckResult(
                name="token",
                ok=False,
                detail="Missing token. Set GATEWAY_BEARER_TOKEN (recommended) or pass --token.",
            )
        )
        return _print_results(results)

    if not base_url:
        if ns.no_start:
            results.append(CheckResult(name="start_server", ok=False, detail="--no-start requires --base-url"))
            return _print_results(results)

        if not token:
            token = "test-token"
            print("NOTE: no token provided; using 'test-token' for the spawned server", file=sys.stderr)

        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"
        obs_url = f"http://127.0.0.1:{port}"

        env = dict(os.environ)
        env.setdefault("GATEWAY_BEARER_TOKEN", token)
        # Keep runtime checks self-contained and fast.
        env.setdefault("MEMORY_ENABLED", "false")
        env.setdefault("MEMORY_V2_ENABLED", "false")
        env.setdefault("METRICS_ENABLED", "true")

        proc = _start_uvicorn(cwd=repo_root, port=port, env=env)
        try:
            _wait_for_health(obs_url or base_url, token)
            results.append(CheckResult(name="start_server", ok=True, detail=base_url))
        except Exception as e:
            results.append(CheckResult(name="start_server", ok=False, detail=f"{type(e).__name__}: {e}"))
            if proc and proc.stderr:
                try:
                    err_tail = (proc.stderr.read() or "")[-4000:]
                    if err_tail.strip():
                        results.append(CheckResult(name="server_stderr", ok=False, detail=err_tail.strip()))
                except Exception:
                    pass
            _stop_process(proc)
            return _print_results(results)

    try:
        resolved_obs = obs_url or _derive_obs_url(base_url)
        http_results = _run_http_checks(
            base_url=base_url,
            obs_url=resolved_obs,
            token=token,
            require_backend=ns.require_backend,
            check_images=bool(ns.check_images),
        )
        results.extend(http_results)
    finally:
        if proc is not None:
            _stop_process(proc)

    return _print_results(results)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
